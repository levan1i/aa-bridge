#!/usr/bin/env python3
"""standalone - the Beagle as a complete Carlinkit-style dongle with NO dongle hardware.

  car (AutoKit app) --USB-C--> [Car: box emulator]  <==>  [AASession: Android Auto head unit]  --Wi-Fi--> phone

* car side : boxd.Box (FunctionFS gadget 1314:1521, encrypted Carlinkit protocol, instant replies)
* phone side: aa_session.AASession (version/TLS/service discovery/channels). The phone is offered 1080p video with
  display insets so the UI is drawn in the TOP 1920x720 of the frame - exactly what the car's app shows.
* video: AA H.264 -> Carlinkit video messages; audio: AA PCM -> Carlinkit audio messages; touch: car -> AA input.
Run as root on the Beagle. Phone: Android Auto 'Start head unit server' (until the Bluetooth start-up works again).
"""
import argparse, asyncio, json, os, struct, sys, threading, time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import boxd
import aa_session as aas
from boxd import log, T_OPEN, T_VIDEO, T_AUDIO, T_CMD, T_BOXSET, T_FILE, T_HB, T_APPINFO, T_CRYPT

import InputEventIndicationMessage_pb2 as inev

DEFAULT_CAR_SIZE = (1920, 720)                                     # until the car's Open says otherwise
T_TOUCH, T_MTOUCH = 5, 0x17
WIRELESS_PORT = 5288


def nal_types(data):
    out, i = [], 0
    while True:
        j = data.find(b"\x00\x00\x01", i)
        if j < 0:
            return out
        if j + 3 < len(data):
            out.append(data[j + 3] & 0x1F)
        i = j + 3


class Car(boxd.Box):
    def __init__(self, app, *a, **k):
        self.app = app
        super().__init__(*a, **k)

    def handle(self, mtype, pay):
        if mtype in (T_HB, T_CRYPT):
            return super().handle(mtype, pay)
        if mtype == T_OPEN:
            vals = struct.unpack("<7I", pay) if len(pay) == 28 else None
            if not vals or not (0 < vals[0] <= 4096 and 0 < vals[1] <= 4096 and 0 < vals[2] <= 120 and vals[3] < 64 and vals[6] < 64):
                log("<- Open garbled (car retry): answering like the last good one")
                pay = self.last_open or struct.pack("<7I", 1920, 720, 30, 5, 49152, 2, 2)
            else:
                log("<- Open %dx%d@%d fmt=%d" % vals[:4])
                self.last_open = pay
                self.app.car_size = (vals[0], vals[1])
            for m in self.ref:                                   # instant startup replies copied from the real dongle
                if m["dir"] == "box" and m["t"] < 0.3 and m["type"] != T_CRYPT:
                    self.send(m["type"], pay if m["type"] == T_OPEN else bytes.fromhex(m["hex"]), plain=not m["enc"])
            self.app.car_opened()
            return
        if mtype in (T_FILE, T_BOXSET, T_APPINFO):
            log("<-", {T_FILE: "SendFile", T_BOXSET: "BoxSettings", T_APPINFO: "AppInfo"}[mtype], pay[:700].decode(errors="replace").replace("\n", " ").replace("\t", ""))
            return
        if mtype == T_CMD and len(pay) >= 4:
            v = struct.unpack("<I", pay[:4])[0]
            log("<- Command", v)
            return
        if mtype in (T_TOUCH, T_MTOUCH):
            self.app.car_touch(mtype, pay)
            return
        if mtype == T_AUDIO:
            return                                               # car microphone: not supported yet
        log("<- type 0x%x len=%d %s" % (mtype, len(pay), pay[:24].hex()))


class App:
    def __init__(self, loop, phone_ip, ref):
        self.loop, self.phone_ip, self.ref = loop, phone_ip, ref
        self.box = None
        self.session = None
        self.car_size = DEFAULT_CAR_SIZE     # display area the car asked for (Open message); decides the phone's video mode
        self.car_is_open = False
        self.car_ready = False
        self.synced = False
        self.cfg = b""                       # SPS/PPS waiting for their IDR
        self.touch_down = {}
        self.sid = 0
        self.vstats = {"in": 0, "out": 0}

    # -------------------------------------------------- car -> app (car thread)
    def car_opened(self):
        self.loop.call_soon_threadsafe(lambda: asyncio.ensure_future(self.sync_car()))

    _tl = 0

    def car_touch(self, mtype, pay):
        App._tl += 1
        if App._tl <= 25:
            log("car touch #%d: type 0x%x len=%d %s" % (App._tl, mtype, len(pay), pay[:48].hex()))
        self.loop.call_soon_threadsafe(lambda: asyncio.ensure_future(self.do_touch(mtype, pay)))

    # -------------------------------------------------- sync of the car with the phone session
    async def sync_car(self):
        self.sid += 1
        sid = self.sid
        self.car_is_open, self.car_ready, self.synced = True, False, False
        await asyncio.sleep(0.4)
        if sid != self.sid:
            return
        if self.session and self.session.alive and self.session.mode != aas.compute_mode(*self.car_size):
            log("car display %dx%d needs another video mode than the running phone session: restarting it" % self.car_size)
            self.session.alive = False                       # the phone reconnects and gets the new mode
            try:
                self.session.w.close()
            except Exception:
                pass
        elif self.session and self.session.alive:
            await self.announce_phone(sid)
        else:
            log("car is ready but no phone session yet; will announce when it connects")

    async def announce_phone(self, sid):
        """Tell the car a phone is connected (the sequence the real dongle sends), then restart the video."""
        n = 0
        for m in self.ref:
            if m["dir"] == "box" and 2.3 < m["t"] < 3.8:
                if sid != self.sid:
                    return
                self.box.send(m["type"], bytes.fromhex(m["hex"]), plain=not m["enc"])
                n += 1
                await asyncio.sleep(0.03)
        log("announced the phone to the car (%d messages); restarting the video stream" % n)
        self.cfg, self.car_ready, self.synced = b"", True, False
        await self.restart_video()

    async def restart_video(self):
        s = self.session
        if not s or not s.alive:
            return
        await s.video_focus(2)               # NATIVE
        await asyncio.sleep(0.3)
        await s.video_focus(1)               # PROJECTED -> the phone starts a fresh stream (SPS/PPS + IDR)

    # -------------------------------------------------- phone -> car
    def on_video(self, ts, data):
        self.vstats["in"] += 1
        types = nal_types(data)
        if not self.car_ready:
            return
        if 7 in types and 5 not in types:                    # codec config on its own: keep for the IDR that follows
            self.cfg = data
            return
        if not self.synced:
            if 5 not in types and 7 not in types:
                return                                       # wait for a keyframe
            self.synced = True
            log("video synced on a keyframe (%d NAL types %s)" % (len(data), types))
        if 5 in types and self.cfg:
            data, self.cfg = self.cfg + data, b""
        m = self.session.mode if self.session else aas.compute_mode(*self.car_size)
        hdr = struct.pack("<5I", m["fw"], m["fh"], 3, (ts // 1000) & 0xFFFFFFFF, 0)
        self.box.send(T_VIDEO, hdr + data)
        self.vstats["out"] += 1

    _aon = {}          # audio_type -> time of last PCM; a stream is "on" once its start command was sent

    def _acmd(self, atype, dec, cmds):
        for c in cmds:                                       # 12-byte header + 1 command byte (as the real dongle sends)
            self.box.send(boxd.T_AUDIO, struct.pack("<IfI", dec, 0.0, atype) + bytes([c]), droppable=False)
        log("audio start commands", atype, cmds)

    def on_audio(self, ch, ts, pcm):
        if not self.car_ready:
            return
        atype, dec = (1, 4) if ch == aas.CH_MEDIA else (2, 5)
        now = time.time()
        if now - self._aon.get(atype, 0) > 1.5:
            self._acmd(atype, dec, (10, 1) if atype == 1 else (6, 1))   # MEDIA_START/OUTPUT_START, or NAVI_START
        self._aon[atype] = now
        self.box.send(boxd.T_AUDIO, struct.pack("<IfI", dec, 0.0, atype) + pcm)

    # -------------------------------------------------- touch car -> phone
    async def do_touch(self, mtype, pay):
        s = self.session
        if not s or not s.alive:
            return
        pts = []                                             # (x_norm, y_norm, action, id) action: 0 down 1 move 2 up
        if mtype == T_MTOUCH:
            for i in range(0, len(pay) - 15, 16):
                x, y, act, pid = struct.unpack("<ffII", pay[i:i + 16])
                pts.append((x, y, act, pid))
        elif mtype == T_TOUCH and len(pay) >= 12:
            act, x, y = struct.unpack("<III", pay[:12])
            pts.append((x / 10000.0, y / 10000.0, {14: 0, 15: 1, 16: 2}.get(act, 1), 0))
        for x, y, act, pid in pts:
            m = s.mode                                       # car x/y (0..1 over the car's display) -> the UI area of the frame
            px = max(0, min(m["uw"] - 1, int(max(0, min(1, x)) * m["uw"])))
            py = max(0, min(m["uh"] - 1, int(max(0, min(1, y)) * m["uh"])))
            await s.send_touch(px, py, act, pid)

    # -------------------------------------------------- phone session loop
    async def run_session(self, r, w, how):
        s = aas.AASession(r, w, on_video=self.on_video, on_audio=self.on_audio, mode=aas.compute_mode(*self.car_size))
        self.session = s
        log("phone session started (%s), video mode for car display %dx%d: res %d frame %dx%d ui %dx%d" % ((how,) + self.car_size + (s.mode["res"], s.mode["fw"], s.mode["fh"], s.mode["uw"], s.mode["uh"])))
        task = asyncio.ensure_future(s.run())
        await s.wait_video_started()
        log("phone session is streaming")
        if self.car_is_open:
            await self.announce_phone(self.sid)
        await task
        log("phone session ended")
        self.session, self.car_ready, self.synced = None, False, False
        try:
            self.box.send(4, b"")                             # Unplugged
        except Exception:
            pass

    async def on_wireless(self, r, w):
        """The phone joined our Wi-Fi after the Bluetooth hand-off and connected to TCP 5288."""
        if self.session and self.session.alive:
            log("second wireless connection while a session runs: refused")
            w.close(); return
        try:
            await self.run_session(r, w, "wireless via Bluetooth")
        except Exception as e:
            log("wireless session error: %r" % e)
            self.session = None

    async def phone_loop(self):
        """Fallback: connect OUT to the phone's 'Start head unit server' (port 5277)."""
        srv = await asyncio.start_server(self.on_wireless, "0.0.0.0", WIRELESS_PORT)
        log("listening for the phone on TCP %d (wireless Android Auto)" % WIRELESS_PORT)
        while True:
            if self.session and self.session.alive:
                await asyncio.sleep(2); continue
            ip = self.phone_ip or aa_newest_lease()
            if not ip:
                await asyncio.sleep(2); continue
            try:
                r, w = await asyncio.wait_for(asyncio.open_connection(ip, 5277), 6)
            except (OSError, asyncio.TimeoutError):
                await asyncio.sleep(2); continue
            try:
                await self.run_session(r, w, "head unit server")
            except Exception as e:
                log("session error: %r" % e)
                self.session = None
            await asyncio.sleep(2)


def aa_newest_lease():
    try:
        rows = sorted((l.split() for l in open("/var/lib/misc/dnsmasq.leases")), key=lambda r: int(r[0]))
        return rows[-1][2]
    except Exception:
        return None


async def amain(a):
    loop = asyncio.get_running_loop()
    ref = json.load(open(a.ref))
    app = App(loop, a.phone, ref)
    ep0 = boxd.setup_gadget(a.udc)
    ep_in = os.open(boxd.FFS + "/ep1", os.O_WRONLY)
    ep_out = os.open(boxd.FFS + "/ep2", os.O_RDONLY)
    box = Car(app, ep_in, ep_out, ref, [], False, 1920, 720, 30)
    app.box = box
    threading.Thread(target=boxd.ep0_events, args=(ep0, box), daemon=True).start()

    def reader():
        while True:
            try:
                data = os.read(ep_out, 65536)
            except OSError:
                time.sleep(0.1); continue
            if data:
                box.feed(data)
    threading.Thread(target=reader, daemon=True).start()
    log("standalone up: car side emulating 1314:1521; phone side connects to the phone's head unit server")
    await app.phone_loop()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--udc", default="31000000.usb")
    ap.add_argument("--ref", default=os.path.join(ROOT, "data", "session_ref.json"))
    ap.add_argument("--phone", default="", help="phone IP (default: newest DHCP lease on the AP)")
    a = ap.parse_args()
    try:
        asyncio.run(amain(a))
    except KeyboardInterrupt:
        pass
    finally:
        boxd.teardown_gadget()
