#!/usr/bin/env python3
"""boxd - emulate a Carlinkit CPC200-CCPA (A15W) 'Auto Box' dongle on a Linux USB gadget port.

Runs on the BeagleY-AI (needs root). It presents the same USB identity and descriptors as the real
dongle (1314:1521, one vendor-specific interface, bulk IN 0x81 / OUT 0x01), speaks the box side of
the Carlinkit/AutoKit protocol (incl. the 0xF0 AES-CFB encryption) and, in --demo mode, pretends a
phone connected and streams a looping H.264 test video so a head-unit app can be tested without a phone.

Protocol notes: docs from the public reverse-engineering projects plus decoded captures of the real
dongle (session_ref.json).
"""
import argparse, json, os, queue, struct, subprocess, sys, threading, time, warnings
warnings.filterwarnings("ignore")
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

GADGET = "/sys/kernel/config/usb_gadget/box"
FFS = "/dev/ffs-box"
BASEKEY = b"SkBRDy3gmrw1ieH0"
MAGIC_PLAIN, MAGIC_ENC = 0x55AA55AA, 0x55BB55BB
T_OPEN, T_PLUGGED, T_PHASE, T_VIDEO, T_AUDIO, T_CMD = 1, 2, 3, 6, 7, 8
T_BOXSET, T_FILE, T_HB, T_VER, T_CRYPT, T_APPINFO = 0x19, 0x99, 0xAA, 0xCC, 0xF0, 0xA0

T0 = time.time()


def log(*a):
    print("%7.2f" % (time.time() - T0), *a, flush=True)


def crypt_cfb(nonce, data, enc):
    key = bytes(BASEKEY[(nonce + i) % 16] for i in range(16))
    iv = bytearray(16)
    iv[1], iv[4], iv[9], iv[12] = nonce & 255, (nonce >> 8) & 255, (nonce >> 16) & 255, (nonce >> 24) & 255
    c = Cipher(algorithms.AES(key), modes.CFB(bytes(iv)))
    return (c.encryptor() if enc else c.decryptor()).update(data)


# ---------------------------------------------------------------- USB gadget (configfs + FunctionFS)
def w(path, val):
    with open(path, "w") as f:
        f.write(str(val))


def sh(*cmd, check=True):
    return subprocess.run(cmd, check=check, capture_output=True, text=True)


def ffs_blobs():
    """FunctionFS descriptor + string blobs: one interface (255/255/0), EP IN 0x81 and EP OUT 0x01, bulk."""
    def intf():
        return struct.pack("<BBBBBBBBB", 9, 4, 0, 0, 2, 255, 255, 0, 1)   # iInterface = FFS string 1

    def ep(addr, mps):
        return struct.pack("<BBBBHB", 7, 5, addr, 2, mps, 0)

    fs = intf() + ep(0x81, 64) + ep(0x01, 64)
    hs = intf() + ep(0x81, 512) + ep(0x01, 512)
    flags = 1 | 2                      # FS + HS descriptors
    body = struct.pack("<II", 3, 3) + fs + hs                      # fs_count, hs_count then the lists
    descs = struct.pack("<III", 3, 12 + len(body), flags) + body   # magic v2, length, flags
    s = "Android Accessory Interface".encode() + b"\0"
    strs = struct.pack("<IIII", 2, 16 + 2 + len(s), 1, 1) + struct.pack("<H", 0x409) + s
    return descs, strs


def setup_gadget(udc):
    if not os.path.isdir("/sys/kernel/config/usb_gadget"):
        sh("modprobe", "libcomposite")
    # free the port: unbind every other configfs gadget
    for g in os.listdir("/sys/kernel/config/usb_gadget"):
        u = "/sys/kernel/config/usb_gadget/%s/UDC" % g
        try:
            if open(u).read().strip():
                w(u, "\n")
        except OSError:
            pass
    if os.path.ismount(FFS):
        sh("umount", FFS, check=False)
    os.makedirs(GADGET + "/strings/0x409", exist_ok=True)
    os.makedirs(GADGET + "/configs/c.1/strings/0x409", exist_ok=True)
    os.makedirs(GADGET + "/functions/ffs.box", exist_ok=True)
    w(GADGET + "/idVendor", "0x1314"); w(GADGET + "/idProduct", "0x1521")
    w(GADGET + "/bcdUSB", "0x0210"); w(GADGET + "/bcdDevice", "0x0409")
    w(GADGET + "/bDeviceClass", "0x00"); w(GADGET + "/bDeviceSubClass", "0x00"); w(GADGET + "/bDeviceProtocol", "0x00")
    w(GADGET + "/strings/0x409/manufacturer", "Magic Communication Tec.")
    w(GADGET + "/strings/0x409/product", "Auto Box")
    w(GADGET + "/strings/0x409/serialnumber", "0123456789FEDCBA")
    w(GADGET + "/configs/c.1/strings/0x409/configuration", "accessory")
    w(GADGET + "/configs/c.1/MaxPower", "500"); w(GADGET + "/configs/c.1/bmAttributes", "0x80")
    link = GADGET + "/configs/c.1/ffs.box"
    if not os.path.islink(link):
        os.symlink(GADGET + "/functions/ffs.box", link)
    os.makedirs(FFS, exist_ok=True)
    sh("mount", "-t", "functionfs", "box", FFS)
    ep0 = os.open(FFS + "/ep0", os.O_RDWR)
    descs, strs = ffs_blobs()
    os.write(ep0, descs)
    os.write(ep0, strs)
    w(GADGET + "/UDC", udc)
    return ep0


def teardown_gadget():
    try:
        w(GADGET + "/UDC", "\n")
    except OSError:
        pass


# ---------------------------------------------------------------- H.264 test video
def load_video(path):
    """Split an Annex-B stream into access units (SPS/PPS/SEI are glued to the following slice)."""
    data = open(path, "rb").read()
    idx, i = [], 0
    while True:
        j = data.find(b"\x00\x00\x01", i)
        if j < 0:
            break
        idx.append(j - 1 if j > 0 and data[j - 1] == 0 else j)
        i = j + 3
    aus, cur = [], b""
    for k, s in enumerate(idx):
        e = idx[k + 1] if k + 1 < len(idx) else len(data)
        nal = data[s:e]
        sc = 4 if nal[:4] == b"\x00\x00\x00\x01" else 3
        t = nal[sc] & 0x1F
        cur += nal
        if t in (1, 5):
            aus.append((t == 5, cur)); cur = b""
    return aus


# ---------------------------------------------------------------- the box
class Box:
    def __init__(self, ep_in, ep_out, ref, video, demo, width, height, fps):
        self.ep_in, self.ep_out, self.ref, self.video, self.demo = ep_in, ep_out, ref, video, demo
        self.width, self.height, self.fps = width, height, fps
        self.lock = threading.Lock()
        self.txq = queue.PriorityQueue()
        self.txseq = 0
        self.last_tx_ok = 0.0
        self.overflow = False
        self.reset()
        threading.Thread(target=self.tx_loop, daemon=True).start()

    def reset(self):
        q = getattr(self, "txq", None)
        if q is not None:                       # host reset/reconnect: forget everything still queued
            try:
                while True:
                    q.get_nowait()
            except queue.Empty:
                pass
        self.nonce = None
        self.crypt = False
        self.buf = b""
        self.last_open = None
        self.session = 0              # bumps on every new Open so old demo threads stop
        self.last_hb = time.time()
        self.hb_count = 0

    # -- sending
    def send(self, mtype, payload=b"", plain=False, droppable=None):
        """Queue a message for the car. A stalled car must never block the caller: live media is dropped
        (and `overflow` set so the bridge can resync) when too much is waiting."""
        magic = MAGIC_PLAIN
        if self.crypt and payload and not plain and mtype not in (T_VIDEO, T_AUDIO):
            magic = MAGIC_ENC
            payload = crypt_cfb(self.nonce, payload, True)
        hdr = struct.pack("<IIII", magic, len(payload), mtype, (~mtype) & 0xFFFFFFFF)
        if droppable is None:
            droppable = mtype in (T_VIDEO, T_AUDIO, 0x2A)
        if droppable and self.txq.qsize() > 300:
            self.overflow = True
            return
        self.txseq += 1
        self.txq.put((1 if droppable else 0, self.txseq, hdr, payload))

    def tx_loop(self):
        while True:
            _, _, hdr, payload = self.txq.get()
            try:
                os.write(self.ep_in, hdr)
                if payload:
                    os.write(self.ep_in, payload)
                self.last_tx_ok = time.time()
            except OSError:
                pass                              # host gone / reset: drop

    # -- receiving
    def feed(self, data):
        self.buf += data
        while len(self.buf) >= 16:
            magic, length, mtype, chk = struct.unpack("<IIII", self.buf[:16])
            if magic not in (MAGIC_PLAIN, MAGIC_ENC) or (mtype ^ chk) != 0xFFFFFFFF or length > 8 << 20:
                log("!! bad header, resync", self.buf[:16].hex()); self.buf = self.buf[1:]; continue
            if len(self.buf) < 16 + length:
                return
            pay = self.buf[16:16 + length]; self.buf = self.buf[16 + length:]
            if magic == MAGIC_ENC and self.nonce and pay:
                pay = crypt_cfb(self.nonce, pay, False)
            self.handle(mtype, pay)

    def handle(self, mtype, pay):
        if mtype == T_HB:
            now = time.time()
            if now - self.last_hb > 3.5:
                log("heartbeat gap %.1f s" % (now - self.last_hb))
            self.last_hb = now; self.hb_count += 1
            if self.hb_count % 15 == 0:
                log("heartbeats: %d (session %d)" % (self.hb_count, self.session))
            return
        if mtype == T_CRYPT and len(pay) == 4:
            n = struct.unpack("<i", pay)[0]
            if n > 0:
                if n != self.nonce:           # a fresh key = the app restarted its session: stop any old demo stream
                    self.session += 1
                self.nonce = n
                self.send(T_CRYPT, b"", plain=True)     # empty plain ack; everything after is encrypted
                self.crypt = True
                log("<- F0 nonce=%d  -> ack" % n)
            else:
                log("<- F0 ignored, bad nonce", n)
            return
        if mtype == T_FILE and len(pay) >= 4:
            nl = struct.unpack("<I", pay[:4])[0]
            name = pay[4:4 + nl].rstrip(b"\0").decode(errors="replace")
            log("<- SendFile", name, pay[8 + nl:].hex()[:16]); return
        if mtype == T_OPEN:
            self.on_open(pay); return
        if mtype == T_BOXSET:
            log("<- BoxSettings", pay[:700].decode(errors="replace").replace("\n", " ").replace("\t", "")); return
        if mtype == T_APPINFO:
            log("<- AppInfo", pay[:400].decode(errors="replace").replace("\n", " ").replace("\t", "")); return
        if mtype == T_CMD and len(pay) >= 4:
            v = struct.unpack("<I", pay[:4])[0]
            log("<- Command", v)
            if v == 12 and self.demo:      # keyframe request
                self.want_key = True
            return
        log("<- type 0x%x len=%d" % (mtype, len(pay)))

    def on_open(self, pay):
        ok = len(pay) == 28
        vals = struct.unpack("<7I", pay) if ok else ()
        # defend against the garbled Open the IM L6 app sends on its retry (huge width/format): ignore it
        if not ok or not (0 < vals[0] <= 4096 and 0 < vals[1] <= 4096 and 0 < vals[2] <= 120 and vals[3] < 64 and vals[6] < 64):
            log("<- Open INVALID, ignored:", pay.hex()); return
        log("<- Open %dx%d@%d fmt=%d" % vals[:4])
        self.width, self.height, self.fps = vals[0], vals[1], vals[2]
        self.last_open = pay
        self.session += 1
        sess = self.session
        # startup replies, copied from the real dongle (decoded capture), sent within milliseconds
        for m in self.ref:
            if m["dir"] == "box" and m["t"] < 0.3 and m["type"] != T_CRYPT:
                body = bytes.fromhex(m["hex"])
                if m["type"] == T_OPEN:
                    body = pay
                self.send(m["type"], body, plain=not m["enc"])
        if self.demo:
            threading.Thread(target=self.demo_phone, args=(sess,), daemon=True).start()

    def demo_phone(self, sess):
        """Pretend a phone connected, then stream the looping test video."""
        t0 = time.time()
        for m in self.ref:
            if m["dir"] == "box" and 2.3 < m["t"] < 3.8:
                d = t0 + (m["t"] - 2.3) * 0.5 + 0.5 - time.time()
                if d > 0:
                    time.sleep(d)
                if sess != self.session:
                    return
                self.send(m["type"], bytes.fromhex(m["hex"]), plain=not m["enc"])
        if not self.video:
            return
        log("demo: streaming %d access units" % len(self.video))
        self.want_key = True
        i, nxt = 0, time.time()
        while sess == self.session:
            key, au = self.video[i % len(self.video)]
            vw, vh = getattr(self, "vsize", (self.width, self.height))
            hdr = struct.pack("<IIIII", vw, vh, 1 if key else 0, len(au), 0)
            try:
                self.send(T_VIDEO, hdr + au)
            except OSError:
                return
            i += 1
            nxt += 1.0 / max(self.fps, 1)
            d = nxt - time.time()
            if d > 0:
                time.sleep(d)
            else:
                nxt = time.time()


def ep0_events(ep0, box):
    """Log and acknowledge FunctionFS control events."""
    names = {0: "BIND", 1: "UNBIND", 2: "ENABLE", 3: "DISABLE", 4: "SETUP", 5: "SUSPEND", 6: "RESUME"}
    while True:
        try:
            ev = os.read(ep0, 12)
        except OSError as e:
            log("ep0 read error", e); time.sleep(0.2); continue
        if len(ev) < 12:
            continue
        setup, typ = ev[:8], ev[8]
        log("ep0 event", names.get(typ, typ), setup.hex() if typ == 4 else "")
        if typ == 2:
            box.reset()
        elif typ == 3:
            log("host disconnected / reset after %d heartbeats" % box.hb_count)
        elif typ == 4:
            bm, br, wv, wi, wl = struct.unpack("<BBHHH", setup)
            try:                                  # no vendor/class handlers: stall
                if bm & 0x80:
                    os.read(ep0, 0)
                else:
                    os.write(ep0, b"")
            except OSError:
                pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--udc", default="31000000.usb")
    ap.add_argument("--ref", default="session_ref.json")
    ap.add_argument("--video", default="")
    ap.add_argument("--vsize", default="", help="WxH written in the video header (default: the size the car asked for)")
    ap.add_argument("--demo", action="store_true", help="pretend a phone connected and stream the test video")
    a = ap.parse_args()
    ref = json.load(open(a.ref))
    video = load_video(a.video) if a.video else []
    log("video access units:", len(video))
    ep0 = setup_gadget(a.udc)
    ep_in = os.open(FFS + "/ep1", os.O_WRONLY)       # first endpoint descriptor = IN
    ep_out = os.open(FFS + "/ep2", os.O_RDONLY)      # second = OUT
    box = Box(ep_in, ep_out, ref, video, a.demo, 1920, 720, 30)
    if a.vsize:
        box.vsize = tuple(int(x) for x in a.vsize.split("x"))
    threading.Thread(target=ep0_events, args=(ep0, box), daemon=True).start()
    log("gadget up as 1314:1521 'Auto Box'; waiting for the head unit")
    try:
        while True:
            try:
                data = os.read(ep_out, 65536)
            except OSError as e:
                time.sleep(0.1); continue
            if data:
                box.feed(data)
    except KeyboardInterrupt:
        pass
    finally:
        teardown_gadget()


if __name__ == "__main__":
    main()
