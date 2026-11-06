#!/usr/bin/env python3
"""Stand-in for the car over USB: plug a real box (or a Beagle gadget) into this machine and it logs every frame the box sends.

    fakecar.py [seconds] [output directory]        defaults: 600, ./fakecar-out

It repeats the startup the IM L6 sent in the relay captures, frame for frame, then sends heartbeats and asks for a keyframe once
video starts. Output: fakecar.log (decoded, also on stdout), frames.jsonl (every frame except video and PCM, in full), video.h264,
audio-<decodeType>-<audioType>.pcm, and cover-N.bin for each cover. Needs pyusb and libusb-1.0.
"""
import json, os, random, signal, struct, sys, threading, time, warnings
warnings.filterwarnings("ignore")
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

BASEKEY = b"SkBRDy3gmrw1ieH0"
PLAIN, ENCRYPTED = 0x55AA55AA, 0x55BB55BB
IDS = ((0x1314, 0x1521), (0x1314, 0x1520))
HEARTBEAT = 2
READ_SIZE = 65536                # one read can hold a header and its payload, whichever way the box splits them
READ_TIMEOUT = 5000              # ms; a read that times out drops what it had, and the box's own heartbeat ends any read before this

# what the car sent, from the relay captures; a box stores these settings, so they stay the ones it already has
OPEN = struct.pack("<7I", 1920, 720, 30, 5, 49152, 2, 2)
BOX_SETTINGS = """{
\t"drivePosition":\t0,
\t"androidWorkMode":\t1,
\t"fps":\t30,
\t"gps":\t30,
\t"lang":\t0,
\t"bgMode":\t0,
\t"startDelay":\t0,
\t"mediaDelay":\t1000,
\t"mediaSound":\t1,
\t"autoConn":\t1,
\t"androidAutoSizeW":\t0,
\t"androidAutoSizeH":\t0,
\t"screenPhysicalW":\t0,
\t"screenPhysicalH":\t0,
\t"ScreenDPI":\t0,
\t"WiFiChannel":\t36,
\t"UseBTPhone":\t0,
\t"HiCarConnectMode":\t0,
\t"GNSSCapability":\t0,
\t"btName":\t"BoxBt-35f7",
\t"wifiName":\t"BoxWifi-35f7",
\t"brand":\t"Volkswagen",
\t"boxName":\t"AutoBox"
}"""
APP_INFO = """{
\t"version":\t"2024.09.13.0957",
\t"platform":\t"aarch64",
\t"model":\t"5GG035280D",
\t"size":\t"1920x720",
\t"android":\t"5.4",
\t"uuid":\t"00000000000000000000000000000000",
\t"huid":\t"linux_0000000000000000",
\t"lang":\t"0"
}"""
SETTLE = 1.3                     # the car's gap between Open and BoxSettings

TYPES = {1: "Open", 2: "Plugged", 3: "Phase", 4: "Unplugged", 5: "Touch", 6: "Video", 7: "Audio", 8: "Command", 0x17: "MultiTouch",
         0x19: "BoxSettings", 0x25: "UiHide", 0x26: "UiBring", 0x2A: "MetaData", 0x99: "SendFile", 0xA0: "AppInfo", 0xA1: "Log",
         0xAA: "Heartbeat", 0xCC: "Version", 0xF0: "Encrypt"}
LINKS = {1: "Android mirror", 3: "CarPlay", 4: "iPhone mirror", 5: "Android Auto", 6: "HiCar"}
COMMANDS = {1: "record start", 2: "record stop", 12: "keyframe", 16: "night", 17: "day", 200: "home", 201: "play", 202: "pause",
            203: "play/pause", 204: "next", 205: "previous", 300: "answer", 301: "hang up", 500: "video focus", 501: "video release",
            502: "audio focus request", 503: "audio focus transient", 504: "audio focus duck", 505: "audio focus release",
            506: "navigation focus request", 507: "navigation focus release"}
AUDIO_COMMANDS = {1: "output start", 2: "output stop", 3: "input config", 4: "call start", 5: "call stop", 6: "navigation start",
                  7: "navigation stop", 8: "voice start", 9: "voice stop", 10: "media start", 11: "media stop", 12: "alert start",
                  13: "alert stop", 14: "incoming call"}


def crypt(nonce, data, encrypt):
    key = bytes(BASEKEY[(nonce + i) % 16] for i in range(16))
    iv = bytearray(16)
    iv[1], iv[4], iv[9], iv[12] = nonce & 255, (nonce >> 8) & 255, (nonce >> 16) & 255, (nonce >> 24) & 255
    c = Cipher(algorithms.AES(key), modes.CFB(bytes(iv)))
    o = c.encryptor() if encrypt else c.decryptor()
    return o.update(data)


def frame(mtype, payload, magic=PLAIN):
    return struct.pack("<IIII", magic, len(payload), mtype, ~mtype & 0xFFFFFFFF) + payload


def send_file(name, content):
    n = name.encode() + b"\0"
    return struct.pack("<I", len(n)) + n + struct.pack("<I", len(content)) + content


class Stream:
    """Cuts USB reads into frames. Junk before a header is skipped and reported, so one lost read costs one frame."""

    def __init__(self):
        self.buf = bytearray()
        self.skipped = 0

    def feed(self, data):
        self.buf += data
        while True:
            at = self.header()
            if at < 0:
                keep = min(len(self.buf), 15)                  # the start of a header may still be arriving
                self.skipped += len(self.buf) - keep
                del self.buf[:len(self.buf) - keep]
                return
            self.skipped += at
            del self.buf[:at]
            if len(self.buf) < 16:
                return
            magic, length, mtype, _ = struct.unpack_from("<IIII", self.buf)
            if len(self.buf) < 16 + length:
                return
            payload = bytes(self.buf[16:16 + length])
            del self.buf[:16 + length]
            yield magic, mtype, payload

    def header(self):
        """Offset of the first plausible header, or of a partial one at the end; -1 if there is none."""
        for at in range(len(self.buf)):
            head = self.buf[at:at + 16]
            for magic in (b"\xaa\x55\xaa\x55", b"\xbb\x55\xbb\x55"):
                if not magic.startswith(bytes(head[:4])):
                    continue
                if len(head) < 16:
                    return at
                _, length, mtype, check = struct.unpack("<IIII", head)
                if check == ~mtype & 0xFFFFFFFF and length < 1 << 24:
                    return at
        return -1


def text(payload):
    t = payload.rstrip(b"\0").decode(errors="replace")
    return " ".join(t.split()) if t.isprintable() or "\n" in t or "\t" in t else payload[:96].hex()


def describe(mtype, payload):
    """One readable line for any frame except video."""
    name = TYPES.get(mtype, "type %#x" % mtype)
    if mtype == 2 and len(payload) >= 4:
        v = struct.unpack("<%dI" % (len(payload) // 4), payload[:len(payload) // 4 * 4])
        return "%s %s%s" % (name, LINKS.get(v[0], v[0]), " wireless" if v[1:2] == (1,) else "")
    if mtype == 3 and len(payload) >= 4:
        return "%s %d" % (name, struct.unpack("<I", payload[:4])[0])
    if mtype == 8 and len(payload) >= 4:
        v = struct.unpack("<I", payload[:4])[0]
        return "%s %d %s" % (name, v, COMMANDS.get(v, ""))
    if mtype == 7 and len(payload) >= 12:
        decode, volume, kind = struct.unpack("<IfI", payload[:12])
        what = "%d %s" % (payload[12], AUDIO_COMMANDS.get(payload[12], "")) if len(payload) == 13 else "%d PCM bytes" % (len(payload) - 12)
        return "%s decode %d volume %g type %d: %s" % (name, decode, volume, kind, what)
    if mtype == 0x2A and len(payload) >= 4:
        sub = struct.unpack("<I", payload[:4])[0]
        body = payload[4:]
        return "%s subtype %d: %s" % (name, sub, "%d bytes of cover" % len(body) if sub == 2 else text(body))
    return "%s (%d bytes) %s" % (name, len(payload), text(payload))


class Output:
    def __init__(self, folder):
        os.makedirs(folder, exist_ok=True)
        self.folder = folder
        self.t0 = time.time()
        self.logf = open(self.path("fakecar.log"), "a", buffering=1)
        self.frames = open(self.path("frames.jsonl"), "a", buffering=1)
        self.video = open(self.path("video.h264"), "ab")
        self.pcm = {}
        self.covers = 0

    def path(self, name):
        return os.path.join(self.folder, name)

    def log(self, *a):
        line = "%8.2f %s" % (time.time() - self.t0, " ".join(str(x) for x in a))
        print(line, flush=True)
        self.logf.write(line + "\n")

    def record(self, mtype, encrypted, payload):
        self.frames.write(json.dumps({"t": round(time.time() - self.t0, 3), "type": mtype, "encrypted": encrypted, "hex": payload.hex()}) + "\n")

    def audio(self, decode, kind, pcm):
        key = (decode, kind)
        if key not in self.pcm:
            self.pcm[key] = open(self.path("audio-%d-%d.pcm" % key), "ab")
        self.pcm[key].write(pcm)

    def cover(self, data):
        self.covers += 1
        with open(self.path("cover-%d.bin" % self.covers), "wb") as f:
            f.write(data)


class Car:
    """One USB session with the box. handle() takes each frame; write() is the only part that touches USB, so it can be replaced."""

    def __init__(self, write, out):
        self.raw_write = write
        self.out = out
        self.lock = threading.Lock()
        self.nonce = random.randint(1, 2**31 - 1)
        self.encrypted = False
        self.alive = True
        self.video_frames = self.video_bytes = self.audio_bytes = self.heartbeats = 0
        self.audio_seen = set()
        self.asked_keyframe = False

    def send(self, mtype, payload=b""):
        magic = PLAIN
        if self.encrypted and payload:
            magic, payload = ENCRYPTED, crypt(self.nonce, payload, True)
        data = frame(mtype, payload, magic)
        with self.lock:
            self.raw_write(data[:16])                           # the car sends the header and the payload as two transfers
            if payload:
                self.raw_write(data[16:])

    def start(self):
        self.send(0xF0, struct.pack("<I", self.nonce))
        self.send(0x99, send_file("/etc/android_work_mode", struct.pack("<I", 1)))
        self.encrypted = True
        self.send(1, OPEN)
        time.sleep(SETTLE)
        self.send(0x19, BOX_SETTINGS.encode())
        self.send(0xA0, APP_INFO.encode())
        self.out.log("startup sent, nonce %#x" % self.nonce)
        threading.Thread(target=self.beat, daemon=True).start()

    def beat(self):
        while self.alive:
            try:
                self.send(0xAA)
            except Exception as e:
                self.out.log("heartbeat stopped: %r" % e)
                return
            time.sleep(HEARTBEAT)

    def handle(self, magic, mtype, payload):
        encrypted = magic == ENCRYPTED
        if encrypted and payload:
            payload = crypt(self.nonce, payload, False)
        if mtype == 0xAA:
            self.heartbeats += 1
        elif mtype == 6:
            self.video(payload)
        elif mtype == 7 and len(payload) > 13:
            decode, _, kind = struct.unpack("<IfI", payload[:12])
            self.audio_bytes += len(payload) - 12
            self.out.audio(decode, kind, payload[12:])
            if (decode, kind) not in self.audio_seen:
                self.audio_seen.add((decode, kind))
                self.out.log("<-", describe(mtype, payload), "(first of this kind)")
        else:
            self.out.record(mtype, encrypted, payload)
            self.out.log("<-", "[enc]" if encrypted else "[plain]", describe(mtype, payload))
            if mtype == 0x2A and len(payload) > 4 and struct.unpack("<I", payload[:4])[0] == 2:
                self.out.cover(payload[4:])

    def video(self, payload):
        self.video_frames += 1
        self.video_bytes += len(payload)
        if self.video_frames == 1 and len(payload) >= 20:
            self.out.log("<- first video frame, header", struct.unpack("<5I", payload[:20]))
        self.out.video.write(payload[20:])
        if not self.asked_keyframe:
            self.asked_keyframe = True
            self.send(8, struct.pack("<I", 12))
            self.out.log("-> Command 12 keyframe")

    def stats(self):
        return "video %d frames %d bytes, PCM %d bytes, heartbeats %d" % (self.video_frames, self.video_bytes, self.audio_bytes, self.heartbeats)


def find_box(usb, deadline, stop, out):
    told = False
    while time.time() < deadline and not stop.is_set():
        for vendor, product in IDS:
            dev = usb.core.find(idVendor=vendor, idProduct=product)
            if dev:
                out.log("box found: %04x:%04x" % (vendor, product))
                return dev
        if not told:
            told = True
            out.log("waiting for the box on USB")
        time.sleep(0.5)


def open_box(usb, dev):
    try:
        dev.get_active_configuration()
    except usb.core.USBError:
        dev.set_configuration()
    intf = dev.get_active_configuration()[(0, 0)]
    if dev.is_kernel_driver_active(0):
        dev.detach_kernel_driver(0)
    usb.util.claim_interface(dev, 0)
    direction = usb.util.endpoint_direction
    ein = usb.util.find_descriptor(intf, custom_match=lambda e: direction(e.bEndpointAddress) == usb.util.ENDPOINT_IN)
    eout = usb.util.find_descriptor(intf, custom_match=lambda e: direction(e.bEndpointAddress) == usb.util.ENDPOINT_OUT)
    return ein, eout


def session(usb, dev, deadline, stop, out):
    """Talk to the box until the time is up or USB goes away. A box that re-enumerates gets a new session."""
    ein, eout = open_box(usb, dev)
    car = Car(lambda data: eout.write(data, timeout=2000), out)
    stream = Stream()
    last = time.time()
    try:
        car.start()
        while time.time() < deadline and not stop.is_set():
            try:
                data = bytes(ein.read(READ_SIZE, timeout=READ_TIMEOUT))
            except usb.core.USBTimeoutError:
                out.log("nothing from the box for %d s" % (READ_TIMEOUT // 1000))
                continue
            for magic, mtype, payload in stream.feed(data):
                car.handle(magic, mtype, payload)
            if stream.skipped:
                out.log("skipped %d bytes that were not a frame" % stream.skipped)
                stream.skipped = 0
            if time.time() - last > 10:
                last = time.time()
                out.log("stats:", car.stats())
    except usb.core.USBError as e:
        out.log("USB error: %r" % e)
    finally:
        car.alive = False
        out.log("session over:", car.stats())
        usb.util.dispose_resources(dev)


def main():
    import usb.core, usb.util
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 600
    out = Output(sys.argv[2] if len(sys.argv) > 2 else "fakecar-out")
    stop = threading.Event()
    for s in (signal.SIGINT, signal.SIGTERM):
        signal.signal(s, lambda *_: stop.set())
    deadline = time.time() + seconds
    while time.time() < deadline and not stop.is_set():
        dev = find_box(usb, deadline, stop, out)
        if dev:
            session(usb, dev, deadline, stop, out)
            time.sleep(1)
    out.log("done")


if __name__ == "__main__":
    main()
