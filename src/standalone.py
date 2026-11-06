#!/usr/bin/env python3
"""standalone - the Beagle as a complete Carlinkit-style dongle with NO dongle hardware.

  car (AutoKit app) --USB-C--> [Car: box emulator]  <==>  [AASession: Android Auto head unit]  --Wi-Fi--> phone

* car side : boxd.Box (FunctionFS gadget 1314:1521, encrypted Carlinkit protocol, instant replies)
* phone side: aa_session.AASession (version/TLS/service discovery/channels). The phone is offered 1080p video with
  display insets so the UI is drawn in the TOP 1920x720 of the frame - exactly what the car's app shows.
* video: AA H.264 -> Carlinkit video messages; audio: AA PCM -> Carlinkit audio messages; touch: car -> AA input.
Run as root on the Beagle. Phone: Android Auto 'Start head unit server' (until the Bluetooth start-up works again).
"""
import argparse, array, asyncio, json, os, socket, struct, sys, threading, time

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


NIGHT_MODE = os.environ.get("AA_NIGHT", "auto")          # auto: the car's own report if any, else by clock; day / night: fixed
NIGHT_FROM = int(os.environ.get("AA_NIGHT_FROM", "18"))  # hour night starts (local time, see AA_TZ)
NIGHT_TO = int(os.environ.get("AA_NIGHT_TO", "7"))       # hour night ends


# Call formats on the car link (3 = 8 kHz mono, 5 = 16 kHz mono), as observed on the test car: 16 kHz playback came out stretched and
# late, an 8 kHz mic sounded robotic to the other side. They differ, so AutoKit's echo canceller is off; callaudio.py ducks the mic instead.
MEDIA_ART_MAX = 512 * 1024
USB_GAP = float(os.environ.get("AA_USB_GAP", "10"))          # seconds off USB after a service restart; see leave_usb_gap
RESTART_MARK = "/run/beagle-aa-bridge.started"               # /run is empty after a boot: a cold start needs no gap
TEST = os.environ.get("AA_TEST", "0") == "1"                 # accept test frames from tools/carpoke.py and keep files the car sends
TEST_PORT = int(os.environ.get("AA_TEST_PORT", "5293"))
FOCUS = os.environ.get("AA_FOCUS", "1") == "1"               # tell the car about the phone's audio and navigation focus
CALL_ALERT = os.environ.get("AA_CALL_ALERT", "1") == "1"     # audio commands 12/13 around the ringing
AUDIO_FOCUS_CMD = {1: 502, 2: 503, 3: 504, 4: 505}           # the firmware's onAudioFocusChanned: request, transient, duck, release
NAV_FOCUS_CMD = {2: 506, 1: 507}                             # onNaviFocusChanned: request, release
CONNECTING = os.environ.get("AA_CONNECTING", "1") == "1"     # show a still picture while no phone is connected
CALL_STATUS = {"IN_CALL": 4, "ON_HOLD": 5, "INCOMING": 2, "CONFERENCED": 4, "MUTED": 3}   # the firmware's table; anything else is 0
CALL_INFO = os.environ.get("AA_CALL_INFO", "1") == "1"   # send the car the caller details message (type 42, subtype 100)
CALL_CTL = ("127.0.0.1", int(os.environ.get("AA_CALL_PORT_CTL", "5292")))    # commands for the Bluetooth headset link (aa_bt)
CALL_PLAY_DEC = int(os.environ.get("AA_CALL_PLAY_DEC", "3"))
CALL_MIC_DEC = int(os.environ.get("AA_CALL_MIC_DEC", "5"))


def double_rate(pcm, last):
    """8 kHz -> 16 kHz mono s16 by linear interpolation; `last` carries the final sample between blocks."""
    a = array.array("h")
    a.frombytes(pcm[:len(pcm) // 2 * 2])
    out = array.array("h")
    for x in a:
        out.append((last + x) // 2)
        out.append(x)
        last = x
    return out.tobytes(), last


def clock_night():
    import datetime
    tz = os.environ.get("AA_TZ", "")
    try:
        import zoneinfo
        h = datetime.datetime.now(zoneinfo.ZoneInfo(tz) if tz else None).hour
    except Exception:
        h = datetime.datetime.now().hour
    return (h >= NIGHT_FROM or h < NIGHT_TO) if NIGHT_FROM > NIGHT_TO else (NIGHT_FROM <= h < NIGHT_TO)


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
            if mtype == T_FILE and TEST:
                self.app.test_keep_file(pay)
            if mtype == T_FILE and b"/tmp/night_mode" in pay and len(pay) >= 4:
                self.app.car_night_file(struct.unpack("<I", pay[-4:])[0])      # 0 day, 1 night, 2 = the box decides
            return
        if mtype == T_CMD and len(pay) >= 4:
            v = struct.unpack("<I", pay[:4])[0]
            log("<- Command", v)
            self.app.car_command(v)
            return
        if mtype in (T_TOUCH, T_MTOUCH):
            self.app.car_touch(mtype, pay)
            return
        if mtype == T_AUDIO:                                     # car microphone: <IfI decodeType, volume, audioType=3> + PCM
            if len(pay) > 12 and struct.unpack("<I", pay[8:12])[0] == 3:
                self.app.car_mic(pay[12:], struct.unpack("<I", pay[:4])[0])
            return
        log("<- type 0x%x len=%d %s" % (mtype, len(pay), pay[:24].hex()))


def notice_lost_phone(w):
    """Without this a phone that left (Wi-Fi off, out of range) keeps its session alive for many minutes: the car stays on the last
    frame and nothing reconnects. Keepalive probes make silence show, and the user timeout closes the connection after 8 s of it."""
    s = w.get_extra_info("socket")
    if s is None:
        return
    s.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    for opt, value in ((socket.TCP_KEEPIDLE, 3), (socket.TCP_KEEPINTVL, 2), (socket.TCP_KEEPCNT, 2), (socket.TCP_USER_TIMEOUT, 8000)):
        s.setsockopt(socket.IPPROTO_TCP, opt, value)


class App:
    def __init__(self, loop, phone_ip, ref):
        self.loop, self.phone_ip, self.ref = loop, phone_ip, ref
        self._call_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
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
        self.mic_on = False                  # the phone has the microphone open and the car was told to record
        self.mic_chunks = 0

    # -------------------------------------------------- car -> app (car thread)
    def car_opened(self):
        self.loop.call_soon_threadsafe(lambda: asyncio.ensure_future(self.sync_car()))

    # -------------------------------------------------- phone calls (the Beagle is the phone's Bluetooth headset; see callaudio.py)
    call_active = False                  # the Bluetooth call audio link is up
    call_mic = False
    call_car = None                      # what the car was told: None, "ringing" (14) or "active" (4)
    call_out = False                     # the car was told OUTPUT_START for the ringing or the call
    call_state = "idle"                  # from the phone's status: "idle", "ringing" or "active"
    call_last = None                     # state in the last caller details sent; the rest as in the firmware's phoneStatusUpdate
    call_incoming = False                # this call rang here
    call_number = ""
    call_name = ""

    def call_audio_cmd(self, *cmds):
        self._alast = time.time()                            # the car answers these with its own play/pause too
        for c in cmds:
            self.box.send(T_AUDIO, struct.pack("<IfI", CALL_PLAY_DEC, 0.0, 1) + bytes([c]), droppable=False)

    def call_alert(self, c):
        """ALERT_START (12) / ALERT_STOP (13) around the ringing."""
        if CALL_ALERT:
            self.call_audio_cmd(c)

    def call_output(self, on):
        if on != self.call_out:
            self.call_out = on
            self.call_audio_cmd(1 if on else 2)

    def call_audio_on(self):
        """Microphone first, then OUTPUT_START, which is what sets up AutoKit's call player."""
        if not self.mic_on:
            self.box.send(T_AUDIO, struct.pack("<IfI", CALL_MIC_DEC, 0.0, 1) + bytes([3]), droppable=False)
            self.box.send(T_CMD, struct.pack("<I", 1), droppable=False)
            self.mic_on = self.call_mic = True
            self.mic_chunks = 0
        self.call_output(True)

    def call_audio_off(self):
        """OUTPUT_STOP and Command 2. The box sends the Command even if nobody answered, but it must not stop a voice session's recording."""
        if self.call_out:
            self.call_output(False)
            if self.call_mic or not self.mic_on:
                self.box.send(T_CMD, struct.pack("<I", 2), droppable=False)
                self.mic_on = self.call_mic = False

    def call_tell_car(self, want):
        """Keep the car's idea of the call in step, in the order the real box sent in docs/calls.md (each change sent once)."""
        was = self.call_car
        if want == was:
            return
        self.call_car = want
        if was == "ringing" and want == "active":
            self.call_output(False)
            self.call_alert(13)
        elif was == "ringing":
            self.call_alert(13)
            self.call_audio_off()
        elif was == "active":
            self.call_audio_off()
        if want is None:
            self.call_audio_cmd(5)
        else:
            self.on_focus("audio", 2)
            self.call_audio_cmd(14 if want == "ringing" else 4)
            if want == "active":
                self.call_audio_on()
            elif CALL_ALERT:
                self.call_alert(12)
                self.call_output(True)
        log("call: car told %s" % (want or "no call"))

    def call_send_info(self, call):
        """MetaData subtype 100, the firmware's phoneStatusUpdate: every phone update while a call exists, one message when it goes.
        CallID is the number, and the number and name are only there if the phone gave them. No call: the last ones, status 0."""
        state = call["state"] if call else "INACTIVE"
        if not call and self.call_last == "INACTIVE":
            return
        if state == "INCOMING":
            self.call_incoming = True
        info = {"CallStatus": CALL_STATUS.get(state, 0), "CallDirection": 1 if self.call_incoming else 2}
        if state == "INACTIVE":
            self.call_incoming = False
        if call and call["number"] is not None:
            self.call_number = call["number"]
        if call and call["name"] is not None:
            self.call_name = call["name"]
        if not call or call["number"] is not None:
            info.update(CallNumber=self.call_number, CallID=self.call_number)
        if not call or call["name"] is not None:
            info["CallName"] = self.call_name
        if CALL_INFO:
            self.box.send(0x2A, struct.pack("<I", 100) + json.dumps(info, ensure_ascii=False, separators=(",", ":")).encode() + b"\0", plain=True, droppable=False)
            if state != self.call_last:
                log("call: details sent to the car (status %d)" % info["CallStatus"])
        self.call_last = state

    def on_phone_status(self, calls):
        """The phone's call list: the only signal that exists before the audio link, so it drives ringing and hanging up."""
        if not self.box or not self.car_is_open:
            return
        live = [c for c in calls if c["state"] not in ("INACTIVE", "UNKNOWN")]
        ring = [c for c in live if c["state"] == "INCOMING"]
        if ring:
            self.call_state = "ringing"
            self.call_tell_car("active" if self.call_car == "active" else "ringing")     # a second call waits: this one stays active
            self.call_send_info(ring[0])
        elif live:
            self.call_state = "active"
            self.call_tell_car("active")
            self.call_send_info(live[0])
        else:
            self.call_state = "idle"
            self.call_send_info(calls[0] if calls else None)
            if not self.call_active:                         # app calls (Meet...) report nothing: their audio link decides
                self.call_tell_car(None)

    def call_key(self, v):
        """Car answer (300) / hang up (301) -> the phone, by Android key; the headset button if the phone's state does not change."""
        s, state = self.session, self.call_state
        if v == 300 and state != "ringing":
            log("car answer button ignored: nothing is ringing")
            return
        if v == 301 and state == "idle" and not self.call_active:
            log("car hang-up button ignored: no call")
            return
        key = 5 if v == 300 else 6
        log("car call button %d -> Android key %d" % (v, key))
        if s and s.alive:
            asyncio.run_coroutine_threadsafe(s.send_key(key), self.loop)
        # The headset button toggles: while ringing it answers, in a call it hangs up. So never use it to reject a ringing call.
        if (v == 300 and state == "ringing") or (v == 301 and state == "active"):
            self.loop.call_soon_threadsafe(self.loop.call_later, 1.0, self.call_key_fallback, state)

    def call_key_fallback(self, state):
        if self.call_state == state:
            log("call: phone state unchanged after the key, pressing the headset button")
            self._call_sock.sendto(b"CKPD", CALL_CTL)
    CALL_OUT = ("127.0.0.1", int(os.environ.get("AA_CALL_PORT_IN", "5291")))      # car mic -> aa_bt (SCO)

    def call_datagram(self, data):
        """From aa_bt over local UDP: b'S' call audio started, b'A'+PCM 8 kHz mono (the caller), b'E' ended."""
        kind, body = data[:1], data[1:]
        if not self.box or not self.car_is_open:
            return
        if kind == b"S":
            self.call_active = True
            self._call_last = 0
            if self.call_state == "ringing":                 # the ringing itself can come over this link
                self.call_tell_car("ringing")
            else:
                self.call_tell_car("active")
                self.call_audio_on()
            log("call: audio link up")
        elif kind == b"A" and self.call_active and self.call_out:
            if CALL_PLAY_DEC == 5:
                body, self._call_last = double_rate(body, self._call_last)
            self.box.send(T_AUDIO, struct.pack("<IfI", CALL_PLAY_DEC, 0.0, 1) + body)
        elif kind == b"E" and self.call_active:
            self.call_active = False
            if self.call_state == "idle":
                self.call_tell_car(None)
            elif self.call_car == "active":                  # moved to the phone itself: the call goes on without the car
                self.call_audio_off()
            log("call: audio link down")

    def call_mic_audio(self, pcm, dec):
        """Car microphone -> the phone's SCO link (8 kHz mono): 8 kHz passes through, 16 kHz is halved."""
        if self.mic_chunks == 1:
            log("call: car microphone decodeType %d" % dec)
        if dec == 3:
            self._call_sock.sendto(pcm, self.CALL_OUT)
            return
        a = array.array("h")
        a.frombytes(pcm[:len(pcm) // 2 * 2])
        out = array.array("h", [(a[i] + a[i + 1]) // 2 for i in range(0, len(a) - 1, 2)])
        self._call_sock.sendto(out.tobytes(), self.CALL_OUT)

    def car_mic(self, pcm, dec=5):
        self.mic_chunks += 1
        if self.call_active:
            self.call_mic_audio(pcm, dec)
            return
        s = self.session
        if s and s.alive and s.mic_open:
            self.loop.call_soon_threadsafe(lambda: asyncio.ensure_future(s.send_mic(pcm)))

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
        self.show_sid = None                                 # a re-opened car has forgotten any announcement
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
            if self.streaming:                               # otherwise run_session announces it when its video starts
                await self.announce_phone(sid)
        else:
            log("car is ready but no phone session yet; will announce when it connects")
            asyncio.ensure_future(self.show_connecting(sid))

    async def send_announcement(self, sid):
        """The sequence the real dongle sends when a phone connects. False if the car re-opened meanwhile."""
        for m in self.ref:
            if m["dir"] == "box" and 2.3 < m["t"] < 3.8:
                if sid != self.sid:
                    return False
                self.box.send(m["type"], bytes.fromhex(m["hex"]), plain=not m["enc"])
                await asyncio.sleep(0.03)
        return True

    show_sid = None                                      # the car session the connecting picture is running for
    announced = None                                     # the car session the phone was announced to
    streaming = False                                    # the phone session's video has started

    async def show_connecting(self, sid):
        """No phone: the car only shows video once a phone is announced, so announce one and repeat a still keyframe until a real
        phone takes over (announce_phone). Repeating it also covers the car's keyframe requests."""
        path = os.path.join(ROOT, "data", "connecting-%dx%d.h264" % self.car_size)
        if not CONNECTING or self.show_sid == sid or sid != self.sid or (self.session and self.session.alive):
            return
        try:
            frame = open(path, "rb").read()
        except OSError:
            log("no connecting picture for this display: %s" % path)
            return
        self.show_sid = sid
        if not await self.send_announcement(sid):
            return
        log("showing the connecting picture")
        m = aas.compute_mode(*self.car_size)
        while self.show_sid == sid and sid == self.sid:
            self.box.send(T_VIDEO, struct.pack("<5I", m["fw"], m["fh"], 3, int(time.time() * 1000) & 0xFFFFFFFF, 0) + frame)
            await asyncio.sleep(0.5)

    async def announce_phone(self, sid):
        """Tell the car a phone is connected, then restart the video. Once per car session: the car and the phone can come back at
        the same moment, and two announcements running together leave the car's screen black."""
        if self.announced == sid:
            return
        self.announced = sid
        if self.show_sid is not None:                        # the car thinks the picture was a phone: end that one first
            self.show_sid = None
            self.box.send(4, b"")
            await asyncio.sleep(0.5)
        if not await self.send_announcement(sid):
            return
        log("announced the phone to the car; restarting the video stream")
        self.mic_on = False                          # the car app restarted its session: its recorder is off again
        if self.session and self.session.mic_open:
            self.on_mic(True)
        self.cfg, self.car_ready, self.synced = b"", True, False
        for cmd in self.focus_wait:
            self.send_focus(cmd)
        self.focus_wait = ()
        await self.restart_video()

    # -------------------------------------------------- microphone (phone asks -> car records -> PCM to the phone)
    def on_mic(self, opened):
        """The phone opened/closed the microphone channel. AutoKit records only after an INPUT_CONFIG audio message
        (decodeType 5 = 16 kHz mono, audioType 1, command byte 3) and a Command 1; Command 2 stops it."""
        if not self.box or not self.car_is_open:
            return
        if opened and not self.mic_on:
            self.box.send(T_AUDIO, struct.pack("<IfI", 5, 0.0, 1) + bytes([3]), droppable=False)
            self.box.send(T_CMD, struct.pack("<I", 1), droppable=False)
            self.mic_on, self.mic_chunks = True, 0
            log("microphone: phone opened it -> car told to record (INPUT_CONFIG 16 kHz mono + cmd 1)")
        elif not opened and self.mic_on and not self.call_active and not self.call_mic:
            self.box.send(T_CMD, struct.pack("<I", 2), droppable=False)
            self.mic_on = False
            log("microphone: phone closed it -> car told to stop (cmd 2); %d chunks received" % self.mic_chunks)

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
        self._alast = time.time()
        for c in cmds:                                       # 12-byte header + 1 command byte (as the real dongle sends)
            self.box.send(boxd.T_AUDIO, struct.pack("<IfI", dec, 0.0, atype) + bytes([c]), droppable=False)
        log("audio start commands", atype, cmds)

    _astop = {1: ((2,), 2.0), 2: ((7, 2), 1.0)}      # audio_type -> (stop commands: OUTPUT_STOP, NAVI_STOP first for navigation; silence before stopping)
    car_night = None                                     # day/night as explicitly reported by the car (None = left to the box)

    def car_night_file(self, v):
        self.car_night = {0: False, 1: True}.get(v)
        log("car night_mode file = %d (%s)" % (v, "auto: the box decides" if self.car_night is None else "night" if self.car_night else "day"))

    def want_night(self):
        if NIGHT_MODE in ("day", "night"):
            return NIGHT_MODE == "night"
        return self.car_night if self.car_night is not None else clock_night()

    async def update_night(self):
        s = self.session
        n = self.want_night()
        if s and s.alive and s.night != n:
            await s.send_night(n)

    async def night_loop(self):
        while True:
            await asyncio.sleep(30)
            await self.update_night()
    _alast = 0.0                                         # time of the last audio start/stop command we sent to the car
    _aact = {}                                           # audio_type -> decode type of a stream the car was told to start

    def audio_stop(self, atype):
        """Close a stream on the car (otherwise the car stays in the navigation/voice audio state)."""
        dec = self._aact.pop(atype, None)
        self._aon.pop(atype, None)
        if dec is not None and self.box and self.car_is_open:
            if atype == 1:
                self.media_signal(False)
            cmds = self._astop[atype][0]
            self._alast = time.time()
            for c in cmds:
                self.box.send(boxd.T_AUDIO, struct.pack("<IfI", dec, 0.0, atype) + bytes([c]), droppable=False)
            log("audio stop commands", atype, cmds)

    async def audio_watchdog(self):
        while True:
            await asyncio.sleep(0.25)
            now = time.time()
            for atype in list(self._aact):
                if now - self._aon.get(atype, 0) > self._astop[atype][1]:
                    self.audio_stop(atype)

    def on_audio(self, ch, ts, pcm):
        if not self.car_ready:
            return
        atype, dec = (1, 4) if ch == aas.CH_MEDIA else (2, 5)
        now = time.time()
        if now - self._aon.get(atype, 0) > 1.5:
            if atype == 1:
                self.media_signal(True)
            self._acmd(atype, dec, (1,) if atype == 1 else (6, 1))      # OUTPUT_START, after NAVI_START for navigation
        self._aact[atype] = dec
        self._aon[atype] = now
        self.box.send(boxd.T_AUDIO, struct.pack("<IfI", dec, 0.0, atype) + pcm)

    # -------------------------------------------------- what is playing, phone -> car
    media_source = ""
    media_state = None
    media_track = None
    media_art = 0                                        # size of the last cover sent: the firmware's "same cover" test
    media_on = False                                     # the car was told MEDIA_START

    def media_signal(self, on):
        """MEDIA_START / MEDIA_STOP. Unlike OUTPUT_START/STOP these follow the playback state, so a pause is told at once."""
        if on != self.media_on and self.box and self.car_is_open:
            self.media_on = on
            self._alast = time.time()                        # the car answers these with its own play/pause too
            self.box.send(boxd.T_AUDIO, struct.pack("<IfI", 4, 0.0, 1) + bytes([10 if on else 11]), droppable=False)

    def media_json(self, info, droppable):
        """MetaData message, subtype 1: plain JSON + NUL."""
        self.box.send(0x2A, struct.pack("<I", 1) + json.dumps(info, ensure_ascii=False, separators=(",", ":")).encode() + b"\0",
                      plain=True, droppable=droppable)

    test_files = 0

    def test_frame(self, data):
        """One frame from tools/carpoke.py: <I type><B plain> + payload."""
        mtype, plain = struct.unpack("<IB", data[:5])
        if self.box:
            self.box.send(mtype, data[5:], plain=bool(plain), droppable=False)
        log("test frame: type 0x%x, %d bytes: %s" % (mtype, len(data) - 5, data[5:80]))

    def test_keep_file(self, pay):
        self.test_files += 1
        path = "/tmp/car-file-%d.bin" % self.test_files
        with open(path, "wb") as f:
            f.write(pay)
        log("test: file from the car kept as %s (%d bytes)" % (path, len(pay)))

    focus_wait = ()                                      # focus commands from before the car was told about the phone
    focus_last = (None, 0.0)

    def send_focus(self, cmd):
        now = time.time()
        if cmd == self.focus_last[0] and now - self.focus_last[1] < 1:     # a call's 503 and the phone's own request for it
            return
        self.focus_last = (cmd, now)
        self._alast = now                                    # the car may answer with its own play/pause
        self.box.send(T_CMD, struct.pack("<I", cmd), droppable=False)
        log("focus: Command %d sent to the car" % cmd)

    def on_focus(self, kind, value):
        """The phone asked for audio or navigation focus: Commands 502-507, as the firmware does."""
        cmd = (AUDIO_FOCUS_CMD if kind == "audio" else NAV_FOCUS_CMD).get(value)
        if not FOCUS or not cmd:
            return
        if self.box and self.car_ready:
            self.send_focus(cmd)
        else:
            self.focus_wait = tuple(self.focus_wait) + (cmd,)

    def on_media(self, kind, m):
        """The firmware's mediaPlaybackUpdate and mediaMetadataUpdate. Neither has MediaPlayStatus: playing is told by media_signal."""
        if not self.box or not self.car_ready:
            return
        if kind == "status":
            if m["state"] == "PLAYING" and 1 not in self._aact:    # "drop invalid playback_state": no media audio is running
                return
            if m["state"] != self.media_state:
                self.media_state = m["state"]
                self.media_signal(m["state"] == "PLAYING")
                log("media: %s (%s)" % (m["state"], m["source"]))
            info = {}
            if m["source"] is not None and m["source"] != self.media_source:
                info["MediaAPPName"] = self.media_source = m["source"]
            if m["position"] is not None:
                info["MediaSongPlayTime"] = m["position"] * 1000
            self.media_json(info, True)
            return
        info = {k: m[f] for k, f in (("MediaSongName", "title"), ("MediaArtistName", "artist"), ("MediaAlbumName", "album")) if m[f] is not None}
        if m["duration"] is not None:
            info["MediaSongDuration"] = m["duration"] * 1000
        self.media_json(info, False)
        art = m["art"]
        if art and len(art) != self.media_art and len(art) <= MEDIA_ART_MAX:
            self.media_art = len(art)
            self.box.send(0x2A, struct.pack("<I", 2) + art, plain=True, droppable=True)    # always 2, whatever the format; no NUL
        track = (m["title"], m["artist"], m["album"], m["duration"], len(art))
        if track != self.media_track:
            self.media_track = track
            log("media: track details sent to the car (%d bytes of art)" % len(art))

    # -------------------------------------------------- buttons car -> phone
    # box protocol Command value -> Android keycode (steering-wheel / head-unit media keys)
    KEYMAP = {200: 3, 201: 126, 202: 127, 203: 85, 204: 87, 205: 88, 106: 4}   # home, play, pause, play/pause, next, prev, back

    def car_command(self, v):
        if v in (16, 17):                                    # the car's day/night theme
            self.car_night = (v == 16)
            log("car reports %s" % ("night" if self.car_night else "day"))
            self.loop.call_soon_threadsafe(lambda: asyncio.ensure_future(self.update_night()))
            return
        if v in (300, 301):
            self.call_key(v)
            return
        key = self.KEYMAP.get(v)
        if v in (201, 202) and time.time() - self._alast < 1.5:
            log("car play/pause %d right after an audio command: the car's own auto-resume, not forwarded" % v)
            return
        s = self.session
        if key and s and s.alive:
            log("car button %d -> Android key %d" % (v, key))
            asyncio.run_coroutine_threadsafe(s.send_key(key), self.loop)

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
        notice_lost_phone(w)
        s = aas.AASession(r, w, on_video=self.on_video, on_audio=self.on_audio, mode=aas.compute_mode(*self.car_size), on_mic=self.on_mic, night=self.want_night(), on_phone_status=self.on_phone_status, on_media=self.on_media, on_focus=self.on_focus)
        self.session = s
        self.media_track, self.media_state, self.media_source, self.media_art, self.media_on = None, None, "", 0, False
        self.call_last, self.call_incoming, self.call_number, self.call_name = None, False, "", ""
        self.focus_wait = ()
        log("phone session started (%s), video mode for car display %dx%d: res %d frame %dx%d ui %dx%d" % ((how,) + self.car_size + (s.mode["res"], s.mode["fw"], s.mode["fh"], s.mode["uw"], s.mode["uh"])))
        task = asyncio.ensure_future(s.run())
        wd = asyncio.ensure_future(self.audio_watchdog())
        nl = asyncio.ensure_future(self.night_loop())
        started = asyncio.ensure_future(s.wait_video_started())
        await asyncio.wait({task, started}, return_when=asyncio.FIRST_COMPLETED)
        if started.done():
            self.streaming = True
            log("phone session is streaming")
            if self.car_is_open:
                await self.announce_phone(self.sid)
        started.cancel()
        try:
            await task
        except Exception as e:                               # the usual end of a phone that left: the cleanup below still has to run
            log("phone session lost: %r" % e)
        wd.cancel(); nl.cancel()
        self.on_phone_status([])
        for a in list(self._aact):
            self.audio_stop(a)
        log("phone session ended")
        if self.mic_on:
            self.on_mic(False)
        self.session, self.car_ready, self.synced, self.streaming, self.announced = None, False, False, False, None
        try:
            self.box.send(4, b"")                             # Unplugged
        except Exception:
            pass
        if self.car_is_open:
            await asyncio.sleep(1)
            await self.show_connecting(self.sid)

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


async def leave_usb_gap():
    """After a service restart the device is gone from USB for about a second. The car then reconnects at once but leaves an empty
    page in front of the video, and touches do not arrive. Gone for several seconds, it reopens the projection properly."""
    if os.path.exists(RESTART_MARK):
        log("service restart: staying off USB for %g s so the car reopens its screen" % USB_GAP)
        await asyncio.sleep(USB_GAP)
    open(RESTART_MARK, "w").close()


async def amain(a):
    loop = asyncio.get_running_loop()
    ref = json.load(open(a.ref))
    app = App(loop, a.phone, ref)
    if os.environ.get("AA_CALLS", "0") == "1":
        class _Calls(asyncio.DatagramProtocol):
            def datagram_received(self, data, addr):
                app.call_datagram(data)
        await loop.create_datagram_endpoint(_Calls, local_addr=("127.0.0.1", int(os.environ.get("AA_CALL_PORT_OUT", "5290"))))
        log("calls: relay enabled (UDP %s)" % os.environ.get("AA_CALL_PORT_OUT", "5290"))
    await leave_usb_gap()
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
    if TEST:
        class _Test(asyncio.DatagramProtocol):
            def datagram_received(self, data, addr):
                if len(data) >= 5:
                    app.test_frame(data)
        await loop.create_datagram_endpoint(_Test, local_addr=("127.0.0.1", TEST_PORT))
        log("test frames: listening on UDP %d" % TEST_PORT)
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
