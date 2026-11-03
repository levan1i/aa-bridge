#!/usr/bin/env python3
"""Minimal stand-in for the car's AutoKit app: encrypted handshake, Open 1920x720, heartbeats; logs what it gets and
saves the video (H.264) and the audio (raw PCM) it receives."""
import struct, sys, threading, time, random, warnings, json
warnings.filterwarnings("ignore")
import usb.core, usb.util
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
B = b"SkBRDy3gmrw1ieH0"
def cfb(n, d, enc):
    k = bytes(B[(n + i) % 16] for i in range(16)); iv = bytearray(16)
    iv[1], iv[4], iv[9], iv[12] = n & 255, (n >> 8) & 255, (n >> 16) & 255, (n >> 24) & 255
    c = Cipher(algorithms.AES(k), modes.CFB(bytes(iv))); o = c.encryptor() if enc else c.decryptor(); return o.update(d)
secs = float(sys.argv[1]) if len(sys.argv) > 1 else 20
t0 = time.time()
def log(*a): print("%6.2f" % (time.time() - t0), *a, flush=True)
dev = usb.core.find(idVendor=0x1314, idProduct=0x1521) or usb.core.find(idVendor=0x1314, idProduct=0x1520)
if not dev: sys.exit("box not found")
try: dev.get_active_configuration()
except usb.core.USBError: dev.set_configuration()
cfg = dev.get_active_configuration(); intf = cfg[(0, 0)]
if dev.is_kernel_driver_active(0): dev.detach_kernel_driver(0)
usb.util.claim_interface(dev, 0)
ein = usb.util.find_descriptor(intf, custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_IN)
eout = usb.util.find_descriptor(intf, custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_OUT)
nonce = random.randint(1, 2**31 - 1); crypt = False; lock = threading.Lock(); alive = True
def send(t, p=b"", plain=False):
    magic = 0x55AA55AA
    if crypt and p and not plain and t not in (6, 7): magic = 0x55BB55BB; p = cfb(nonce, p, True)
    with lock:
        eout.write(struct.pack("<IIII", magic, len(p), t, (~t) & 0xFFFFFFFF), timeout=2000)
        if p: eout.write(p, timeout=2000)
def sendfile(name, content):
    n = name.encode() + b"\0"; send(0x99, struct.pack("<I", len(n)) + n + struct.pack("<I", len(content)) + content)
def hb():
    while alive:
        try: send(0xAA)
        except Exception: return
        time.sleep(2)
send(0xF0, struct.pack("<I", nonce)); time.sleep(0.1); crypt = True
sendfile("/etc/android_work_mode", struct.pack("<I", 1))
send(0x01, struct.pack("<7I", 1920, 720, 30, 5, 49152, 2, 2))
send(0x19, json.dumps({"androidAutoSizeW": 0, "androidAutoSizeH": 0, "mediaDelay": 1000}).encode())
send(0xA0, json.dumps({"version": "2024.09.13.0957", "model": "fakecar2", "size": "1920x720"}).encode())
threading.Thread(target=hb, daemon=True).start()
vf = open("fc_video.h264", "wb"); af = open("fc_audio.pcm", "wb")
stats = {"video": 0, "vbytes": 0, "audio": 0, "other": {}}; first = {}
def rd(n):
    b = bytearray()
    while len(b) < n:
        c = bytes(ein.read(max(n - len(b), 512) if n - len(b) > 16 else n - len(b), timeout=3000)); b += c
    return bytes(b[:n])
last = time.time()
try:
    while time.time() - t0 < secs:
        try:
            h = bytes(ein.read(16, timeout=1000))
        except usb.core.USBTimeoutError: continue
        if not h: continue
        magic, ln, t, _ = struct.unpack("<IIII", h)
        p = rd(ln) if ln else b""
        if magic == 0x55BB55BB and p: p = cfb(nonce, p, False)
        if t == 6:
            stats["video"] += 1; stats["vbytes"] += len(p)
            if "v" not in first: first["v"] = 1; log("first video frame header:", struct.unpack("<5I", p[:20]))
            vf.write(p[20:])
        elif t == 7:
            stats["audio"] += 1; af.write(p[12:])
            if "a" not in first: first["a"] = 1; log("first audio header:", struct.unpack("<IfI", p[:12]))
        elif t != 0xAA:
            stats["other"][t] = stats["other"].get(t, 0) + 1
            if t == 2: log("PLUGGED phone type", struct.unpack("<I", p[:4])[0])
            elif t == 3: log("Phase", struct.unpack("<I", p[:4])[0])
            elif t == 4: log("UNPLUGGED")
        if time.time() - last > 5:
            last = time.time(); log("stats", stats)
except usb.core.USBError as e:
    log("USB error:", e)
alive = False; log("done", stats)
