#!/usr/bin/env python3
"""aa_session - the Android Auto *head unit* side of one phone connection (TCP, after the wireless handshake).

Protocol (from aasdk / open-android-auto):
  frame   = [channel:1][flags:1][size:2 BE]([total:4 BE] if FIRST)[payload]
  flags   = frame type (3=BULK 1=FIRST 0=MIDDLE 2=LAST) | 0x04 CONTROL | 0x08 ENCRYPTED
  payload = [msg_id:2 BE][protobuf]; encrypted payloads are TLS 1.2 records (the HU is the TLS *client*)
  start   : HU sends VERSION_REQUEST, phone answers; TLS handshake carried in SSL_HANDSHAKE (3) messages;
            HU sends AUTH_COMPLETE; the phone then sends SERVICE_DISCOVERY_REQUEST and opens channels.
"""
import asyncio, os, ssl, struct, sys, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.environ.get("AAGEN", os.path.join(ROOT, "gen")))
import AVChannelData_pb2 as avch
import AVChannelSetupRequestMessage_pb2 as avsetupreq
import AVChannelSetupResponseMessage_pb2 as avsetupresp
import AVChannelStartIndicationMessage_pb2 as avstart
import AVInputChannelData_pb2 as avinch
import AVInputOpenRequestMessage_pb2 as avinopen
import AVInputOpenResponseMessage_pb2 as avinopenresp
import AVMediaAckIndicationMessage_pb2 as avack
import AudioFocusRequestMessage_pb2 as afreq
import AudioFocusResponseMessage_pb2 as afresp
import AuthCompleteIndicationMessage_pb2 as authc
import BindingRequestMessage_pb2 as bindreq
import BindingResponseMessage_pb2 as bindresp
import ChannelDescriptorData_pb2 as chdesc
import ChannelOpenRequestMessage_pb2 as chopenreq
import ChannelOpenResponseMessage_pb2 as chopenresp
import InputChannelData_pb2 as inch
import NavigationFocusRequestMessage_pb2 as navreq
import NavigationFocusResponseMessage_pb2 as navresp
import BluetoothPairingRequestMessage_pb2 as btreq
import BluetoothPairingResponseMessage_pb2 as btresp
import PingRequestMessage_pb2 as pingreq
import PingResponseMessage_pb2 as pingresp
import SensorChannelData_pb2 as sench
import SensorData_pb2 as sensd
import SensorEventIndicationMessage_pb2 as sevent
import SensorStartRequestMessage_pb2 as sstartreq
import SensorStartResponseMessage_pb2 as sstartresp
import ServiceDiscoveryResponseMessage_pb2 as sdresp
import ShutdownRequestMessage_pb2 as shutreq
import ShutdownResponseMessage_pb2 as shutresp
import TouchConfigData_pb2 as touchcfg
import VideoFocusIndicationMessage_pb2 as vfi
import InputEventIndicationMessage_pb2 as inev
import VideoFocusRequestMessage_pb2 as vfr
import AudioConfigData_pb2 as audcfg
import VideoConfigData_pb2 as vidcfg

CERT = os.environ.get("AA_CERT", os.path.join(ROOT, "certs", "hu_cert.pem"))
KEY = os.environ.get("AA_KEY_FILE", os.path.join(ROOT, "certs", "hu_key.pem"))

BULK, FIRST, MIDDLE, LAST = 3, 1, 0, 2
F_CTRL, F_ENC = 0x04, 0x08
MAX_FRAME = 0x4000

CH_CONTROL, CH_INPUT, CH_SENSOR, CH_VIDEO, CH_MEDIA, CH_SPEECH, CH_SYSTEM, CH_MIC, CH_BT, CH_PHONE, CH_MEDIAINFO = 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10
CH_NAMES = {0: "control", 1: "input", 2: "sensor", 3: "video", 4: "media-audio", 5: "speech-audio", 6: "system-audio", 7: "mic", 8: "bluetooth", 9: "phone-status", 10: "media-info"}
MEDIA_INFO = os.environ.get("AA_MEDIA_INFO", "1") == "1"   # ask the phone for what is playing, and pass it on to the car
PLAYBACK_STATES = {1: "STOPPED", 2: "PLAYING", 3: "PAUSED"}
CALLS = os.environ.get("AA_CALLS", "0") == "1"             # the car can really carry calls (see callaudio.py)
CALL_STATES = {0: "UNKNOWN", 1: "IN_CALL", 2: "ON_HOLD", 3: "INACTIVE", 4: "INCOMING", 5: "CONFERENCED", 6: "MUTED"}

# Video mode offered to the phone is derived from the car's Open request (see compute_mode); fps is fixed at 30.
VIDEO_FPS = 1                                              # 1 = 30 fps
TIERS = {1: (800, 480), 2: (1280, 720), 3: (1920, 1080),   # AA video resolutions: 480p, 720p, 1080p
         5: (720, 1280), 6: (1080, 1920), 7: (1440, 2560)}  # portrait ones: 720p_p, 1080p_p, 1440p_p
DPI = int(os.environ.get("AA_DPI", "160"))


def compute_mode(w, h):
    """AA video mode for a car display of w x h: the smallest AA resolution at least w wide (AA_RES forces one).
    The car's w x h area is the top-left part of that frame; the remainder is excluded through margins and display
    insets, so the phone draws its UI only in the area the car shows."""
    forced = os.environ.get("AA_RES", "")
    if forced in ("1", "2", "3", "5", "6", "7"):
        res = int(forced)
    elif h > w:                                              # portrait display
        res = 5 if w <= 720 else 6 if w <= 1080 else 7
    else:
        res = 1 if w <= 800 else 2 if w <= 1280 else 3
    fw, fh = TIERS[res]
    uw, uh = min(w, fw), min(h, fh)
    mw, mh = fw - uw, fh - uh
    return {"res": res, "fw": fw, "fh": fh, "uw": uw, "uh": uh, "margin_w": mw, "margin_h": mh,
            "insets": ("0,%d,0,%d" % (mh, mw)) if (mw or mh) else "", "dpi": DPI}


VER_MAJOR = int(os.environ.get("AA_VER_MAJOR", "1"))
VER_MINOR = int(os.environ.get("AA_VER_MINOR", "1"))
LEFT_HAND_DRIVE = os.environ.get("AA_LHD", "0") == "1"


def _varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F; n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def pb_fields(b):
    """Minimal protobuf reader: yields (field number, value) for varint and length-delimited fields."""
    i = 0
    while i < len(b):
        key, i = _rd_varint(b, i)
        f, w = key >> 3, key & 7
        if w == 0:
            v, i = _rd_varint(b, i)
        elif w == 2:
            n, i = _rd_varint(b, i)
            v, i = b[i:i + n], i + n
        elif w == 1:
            v, i = b[i:i + 8], i + 8
        elif w == 5:
            v, i = b[i:i + 4], i + 4
        else:
            return
        yield f, v


def _rd_varint(b, i):
    v = s = 0
    while True:
        c = b[i]
        i += 1
        v |= (c & 0x7F) << s
        s += 7
        if not c & 0x80:
            return v, i


def pb_text(fields, n):
    """String field n, or None if the message does not have it."""
    return bytes(fields[n]).decode(errors="replace") if n in fields else None


def discovery_model(body):
    """The phone's maker and model from its SERVICE_DISCOVERY_REQUEST: field 5, what the firmware logs as "brand" and sends as MDModel."""
    for f, v in pb_fields(body):
        if f == 5 and isinstance(v, bytes):
            return v.decode(errors="replace").strip()
    return ""


def phone_calls(body):
    """PhoneStatusUpdate -> [{state, seconds, number, name}]; a number or name the phone did not send is None."""
    calls = []
    for f, v in pb_fields(body):
        if f == 1:
            c = dict(pb_fields(v))
            calls.append({"state": CALL_STATES.get(c.get(1), "UNKNOWN"), "seconds": c.get(2, 0), "number": pb_text(c, 3), "name": pb_text(c, 4)})
    return calls


def media_status(body):
    """MediaPlaybackStatus (0x8001) -> {state, source, position}."""
    f = dict(pb_fields(body))
    return {"state": PLAYBACK_STATES.get(f.get(1), "UNKNOWN"), "source": pb_text(f, 2), "position": f.get(3)}


def media_metadata(body):
    """MediaPlaybackMetadata (0x8003) -> {title, artist, album, art, duration}."""
    f = dict(pb_fields(body))
    return {"title": pb_text(f, 1), "artist": pb_text(f, 2), "album": pb_text(f, 3), "art": bytes(f.get(4, b"")), "duration": f.get(6)}


def phone_status_text(calls):
    """For the log: "IN_CALL 12s" per call. Numbers and names are deliberately left out."""
    return ", ".join("%s %ds" % (c["state"], c["seconds"]) for c in calls) or "no calls"


def additional_video_config(insets):
    """Raw protobuf for VideoConfig field 11: AdditionalVideoConfig{ display_insets{top,bottom,left,right} }."""
    top, bottom, left, right = (int(x) for x in insets.split(","))
    ins = b"".join(bytes([(f << 3) | 0]) + _varint(v) for f, v in ((1, top), (2, bottom), (3, left), (4, right)))
    avc = bytes([(1 << 3) | 2]) + _varint(len(ins)) + ins
    return bytes([(11 << 3) | 2]) + _varint(len(avc)) + avc

T0 = time.time()


def log(*a):
    print("%7.2f" % (time.time() - T0), *a, flush=True)


def bt_address():
    import re, subprocess
    try:
        m = re.search(r"BD Address: ([0-9A-F:]{17})", subprocess.run(["hciconfig", "hci0"], capture_output=True, text=True).stdout)
        if m:
            return m.group(1)
    except OSError:
        pass
    return os.environ.get("AA_BT_ADDR", "")


class AASession:
    def __init__(self, reader, writer, on_video=None, on_audio=None, name="phone", mode=None, on_mic=None, night=False, on_phone_status=None, on_media=None, on_focus=None):
        self.on_focus = on_focus                   # called with ("audio" or "navigation", request type)
        self.phone_model = ""                      # from the phone's discovery request
        self.on_media = on_media                   # called with ("status", {...}) or ("metadata", {...}) from the phone
        self.on_phone_status = on_phone_status     # called with the phone's list of calls whenever it reports them
        self.on_mic = on_mic                       # called with True/False when the phone opens/closes the microphone
        self.mic_open = False
        self.night = night
        self.call_avail_sent = False
        self.mode = mode or compute_mode(1920, 720)
        self.r, self.w, self.name = reader, writer, name
        self.on_video, self.on_audio = on_video, on_audio
        self.pending = {}                          # channel -> (flags, bytearray) being reassembled
        self.sessions = {}                         # channel -> AV session id
        self.stats = {"video": 0, "video_bytes": 0, "audio": 0, "touch": 0}
        self.alive = True
        self._last_stats = time.time()
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        ctx.minimum_version = ctx.maximum_version = ssl.TLSVersion.TLSv1_2
        ctx.set_ciphers("ALL:@SECLEVEL=0")
        ctx.load_cert_chain(CERT, KEY)
        self.inb, self.outb = ssl.MemoryBIO(), ssl.MemoryBIO()
        self.tls = ctx.wrap_bio(self.inb, self.outb, server_side=False)

    # ---------------------------------------------------------------- framing
    async def send_frames(self, channel, flags_extra, data):
        total = len(data)
        if total <= MAX_FRAME:
            self.w.write(bytes([channel, BULK | flags_extra]) + struct.pack(">H", total) + data)
        else:
            off, first = 0, True
            while off < total:
                chunk = data[off:off + MAX_FRAME]
                off += len(chunk)
                ft = FIRST if first else (LAST if off >= total else MIDDLE)
                hdr = bytes([channel, ft | flags_extra]) + struct.pack(">H", len(chunk))
                if first:
                    hdr += struct.pack(">I", total)
                self.w.write(hdr + chunk)
                first = False
        await self.w.drain()

    def encrypt(self, plain):
        self.tls.write(plain)
        out = b""
        while True:
            c = self.outb.read()
            if not c:
                break
            out += c
        return out

    def decrypt(self, cipher):
        self.inb.write(cipher)
        out = b""
        while True:
            try:
                p = self.tls.read(65536)
            except ssl.SSLWantReadError:
                break
            if not p:
                break
            out += p
        return out

    async def send_msg(self, channel, msg_id, proto=b"", encrypted=True, control=False):
        plain = struct.pack(">H", msg_id) + (proto if isinstance(proto, bytes) else proto.SerializeToString())
        data = self.encrypt(plain) if encrypted else plain
        await self.send_frames(channel, (F_ENC if encrypted else 0) | (F_CTRL if control else 0), data)

    async def read_message(self):
        """Return (channel, flags, payload_bytes) for the next complete message (payload still encrypted)."""
        while True:
            hdr = await self.r.readexactly(4)
            ch, fl, size = hdr[0], hdr[1], struct.unpack(">H", hdr[2:4])[0]
            ft = fl & 3
            if ft == FIRST:
                await self.r.readexactly(4)                      # total size, not needed
            data = await self.r.readexactly(size)
            if fl & F_ENC:
                data = self.decrypt(data)                        # per frame, in wire order (fragments of different channels interleave)
            if ft == BULK:
                return ch, fl, data
            if ft == FIRST:
                self.pending[ch] = (fl, bytearray(data))
            else:
                if ch not in self.pending:
                    log("fragment without start on channel", ch); continue
                self.pending[ch][1].extend(data)
                if ft == LAST:
                    fl0, buf = self.pending.pop(ch)
                    return ch, fl0, bytes(buf)

    # ---------------------------------------------------------------- session
    async def start(self):
        # 1. version
        await self.send_msg(CH_CONTROL, 1, struct.pack(">HH", VER_MAJOR, VER_MINOR), encrypted=False)
        log("-> VERSION_REQUEST %d.%d" % (VER_MAJOR, VER_MINOR))
        ch, fl, data = await self.read_message()
        mid = struct.unpack(">H", data[:2])[0]
        if mid != 2:
            raise RuntimeError("expected VERSION_RESPONSE, got 0x%04x" % mid)
        major, minor, status = struct.unpack(">HHH", data[2:8])
        log("<- VERSION_RESPONSE %d.%d status=%d" % (major, minor, status))
        if status != 0:
            raise RuntimeError("version mismatch")
        # 2. TLS handshake inside SSL_HANDSHAKE messages
        done = False
        while not done:
            try:
                self.tls.do_handshake()
                done = True
            except ssl.SSLWantReadError:
                pass
            out = b""
            while True:
                c = self.outb.read()
                if not c:
                    break
                out += c
            if out:
                await self.send_frames(CH_CONTROL, 0, struct.pack(">H", 3) + out)
                log("-> SSL_HANDSHAKE %d bytes" % len(out))
            if done:
                break
            ch, fl, data = await self.read_message()
            mid = struct.unpack(">H", data[:2])[0]
            if mid != 3:
                raise RuntimeError("expected SSL_HANDSHAKE, got 0x%04x" % mid)
            log("<- SSL_HANDSHAKE %d bytes" % (len(data) - 2))
            self.inb.write(data[2:])
        log("TLS established:", self.tls.version(), self.tls.cipher()[0])
        # 3. auth complete (plain)
        ac = authc.AuthCompleteIndication(); ac.status = 0
        await self.send_msg(CH_CONTROL, 4, ac, encrypted=False)
        log("-> AUTH_COMPLETE")

    async def run(self):
        try:
            await self.start()
            asyncio.ensure_future(self.pinger())
            while self.alive:
                ch, fl, data = await self.read_message()
                if len(data) < 2:
                    continue
                mid = struct.unpack(">H", data[:2])[0]
                await self.dispatch(ch, fl, mid, data[2:])
        except (asyncio.IncompleteReadError, ConnectionError) as e:
            log("connection closed:", repr(e))
        finally:
            self.alive = False
            try:
                self.w.close()
            except Exception:
                pass
            log("session ended, stats:", self.stats)

    async def video_focus(self, mode):
        """VideoFocusIndication (1 = projected, 2 = native): the phone (re)starts or stops its video stream."""
        fi = vfi.VideoFocusIndication(); fi.focus_mode = mode; fi.unrequested = True
        await self.send_msg(CH_VIDEO, 0x8008, fi)
        log("-> video focus", "PROJECTED" if mode == 1 else "NATIVE")

    async def send_touch(self, x, y, action, pid):
        """action: 0 down, 1 move, 2 up. x/y in the AA frame's pixel space."""
        ev = inev.InputEventIndication()
        ev.timestamp = int(time.time() * 1e6)
        t = ev.touch_event
        loc = t.touch_location.add(); loc.x, loc.y, loc.pointer_id = x, y, pid
        t.touch_action = {0: 0, 1: 2, 2: 1}[action]          # PRESS, DRAG, RELEASE
        await self.send_msg(CH_INPUT, 0x8001, ev)
        self.stats["touch"] += 1

    async def send_night(self, night):
        """Tell the phone the car switched between day and night (Android Auto changes the map and UI theme)."""
        self.night = bool(night)
        if not self.alive:
            return
        ev = sevent.SensorEventIndication()
        ev.night_mode.add().is_night = self.night
        await self.send_msg(CH_SENSOR, 0x8003, ev)
        log("-> night mode %s" % ("ON" if self.night else "off"))

    async def send_key(self, keycode):
        """Press and release one Android key (media/navigation button) on the phone."""
        if not self.alive:
            return
        for down in (True, False):
            ev = inev.InputEventIndication()
            ev.timestamp = int(time.time() * 1e6)
            b = ev.button_event.button_events.add()
            b.scan_code, b.is_pressed, b.meta, b.long_press = keycode, down, 0, False
            await self.send_msg(CH_INPUT, 0x8001, ev)
        self.stats["key"] = self.stats.get("key", 0) + 1

    async def send_mic(self, pcm):
        """Car microphone audio to the phone: 16 kHz, 16-bit, mono PCM as AV media with a timestamp on the mic channel."""
        if not (self.alive and self.mic_open and pcm):
            return
        await self.send_msg(CH_MIC, 0x0000, struct.pack(">Q", int(time.time() * 1e6)) + pcm)
        self.stats["mic"] = self.stats.get("mic", 0) + 1

    async def wait_video_started(self):
        while self.alive and self.stats["video"] == 0:
            await asyncio.sleep(0.1)

    async def shutdown(self):
        """Tell the phone we are leaving (ShutdownRequest reason=QUIT) so it can clean up its projection."""
        try:
            sr = shutreq.ShutdownRequest(); sr.reason = 1
            await self.send_msg(CH_CONTROL, 0x0f, sr)
            log("-> SHUTDOWN_REQUEST (quit)")
            await asyncio.sleep(0.6)
        except Exception as e:
            log("shutdown not sent:", repr(e))

    async def pinger(self):
        while self.alive:
            await asyncio.sleep(5)
            try:
                p = pingreq.PingRequest(); p.timestamp = int(time.time() * 1e6)
                await self.send_msg(CH_CONTROL, 0x0b, p, encrypted=False)
            except Exception:
                return

    # ---------------------------------------------------------------- service discovery
    def build_discovery(self):
        r = sdresp.ServiceDiscoveryResponse()
        r.head_unit_name = "Beagle AA"
        r.car_model = "L6"
        r.car_year = "2024"
        r.car_serial = "0001"
        r.left_hand_drive_vehicle = LEFT_HAND_DRIVE
        r.headunit_manufacturer = "Beagle"
        r.headunit_model = "BeagleY-AI"
        r.sw_build = "1"
        r.sw_version = "1.0"
        r.can_play_native_media_during_vr = False
        # sensors
        c = r.channels.add(); c.channel_id = CH_SENSOR
        for t in (13, 10):                                  # DRIVING_STATUS, NIGHT_DATA
            c.sensor_channel.sensors.add().type = t
        # video
        c = r.channels.add(); c.channel_id = CH_VIDEO
        c.av_channel.stream_type = 3
        v = c.av_channel.video_configs.add()
        m = self.mode
        v.video_resolution, v.video_fps, v.margin_width, v.margin_height, v.dpi = m["res"], VIDEO_FPS, m["margin_w"], m["margin_h"], m["dpi"]
        if m["insets"]:
            v.MergeFromString(additional_video_config(m["insets"]))
        # audio: media (48k stereo), speech and system (16k mono)
        for chid, atype, rate, chans in ((CH_MEDIA, 3, 48000, 2), (CH_SPEECH, 1, 16000, 1), (CH_SYSTEM, 2, 16000, 1)):
            c = r.channels.add(); c.channel_id = chid
            c.av_channel.stream_type = 1
            c.av_channel.audio_type = atype
            a = c.av_channel.audio_configs.add(); a.sample_rate, a.bit_depth, a.channel_count = rate, 16, chans
        # microphone
        c = r.channels.add(); c.channel_id = CH_MIC
        c.av_input_channel.stream_type = 1
        c.av_input_channel.audio_config.sample_rate = 16000
        c.av_input_channel.audio_config.bit_depth = 16
        c.av_input_channel.audio_config.channel_count = 1
        if MEDIA_INFO:                                      # media info: empty descriptor (field 9), a marker with no settings
            c = r.channels.add(); c.channel_id = CH_MEDIAINFO
            c.MergeFromString(b"\x4a\x00")
        if CALLS:                                           # phone status: empty descriptor (field 10), a marker with no settings
            c = r.channels.add(); c.channel_id = CH_PHONE
            c.MergeFromString(b"\x52\x00")
        # bluetooth: wireless Android Auto expects the head unit's Bluetooth address (the phone is paired with it)
        bt = bt_address()
        if bt:
            c = r.channels.add(); c.channel_id = CH_BT
            c.bluetooth_channel.adapter_address = bt
            c.bluetooth_channel.supported_pairing_methods.extend([2, 4])    # A2DP, HFP
        # input: touch screen in the AA video coordinate space
        c = r.channels.add(); c.channel_id = CH_INPUT
        c.input_channel.touch_screen_config.width = self.mode["fw"]
        c.input_channel.touch_screen_config.height = self.mode["fh"]
        c.input_channel.supported_keycodes.extend([3, 4, 5, 6, 84, 85, 86, 87, 88, 89, 90, 91, 126, 127])
        return r

    # ---------------------------------------------------------------- message handling
    async def dispatch(self, ch, fl, mid, body):
        name = CH_NAMES.get(ch, str(ch))
        if ch == CH_CONTROL:
            return await self.on_control(mid, body)
        if mid == 7:                                                    # CHANNEL_OPEN_REQUEST
            q = chopenreq.ChannelOpenRequest(); q.ParseFromString(body)
            log("<- %s CHANNEL_OPEN_REQUEST priority=%d" % (name, q.priority))
            rs = chopenresp.ChannelOpenResponse(); rs.status = 0
            await self.send_msg(ch, 8, rs, control=True)
            return
        if ch == CH_BT:
            if mid == 0x8001:                                           # PAIRING_REQUEST
                q = btreq.BluetoothPairingRequest(); q.ParseFromString(body)
                # Written by hand: the aasdk .proto has these two fields swapped. Real layout: 1 = status (0 SUCCESS), 2 = already_paired.
                await self.send_msg(ch, 0x8002, b"\x08\x00\x10\x01")
                log("<- bluetooth PAIRING_REQUEST phone=%s method=%d -> success, already paired" % (q.phone_address, q.pairing_method))
                if CALLS and not self.call_avail_sent:
                    self.call_avail_sent = True
                    await self.send_msg(CH_CONTROL, 24, b"\x08\x01")          # CallAvailabilityStatus{call_available=true}
                    log("-> CallAvailabilityStatus: calls available")
            return
        if ch == CH_MEDIAINFO:
            if self.on_media and mid == 0x8001:
                self.on_media("status", media_status(body))
            elif self.on_media and mid == 0x8003:
                self.on_media("metadata", media_metadata(body))
            elif mid not in (0x8001, 0x8003):
                log("<- media-info msg 0x%04x (%d bytes)" % (mid, len(body)))
            return
        if ch == CH_PHONE:
            if mid == 0x8001:                                           # PhoneStatusUpdate
                calls = phone_calls(body)
                log("<- phone status:", phone_status_text(calls))
                if self.on_phone_status:
                    self.on_phone_status(calls)
            else:
                log("<- phone-status msg 0x%04x (%d bytes): %s" % (mid, len(body), body[:32].hex()))
            return
        if ch == CH_INPUT:
            if mid == 0x8002:                                           # BINDING_REQUEST
                q = bindreq.BindingRequest(); q.ParseFromString(body)
                rs = bindresp.BindingResponse(); rs.status = 0
                await self.send_msg(ch, 0x8003, rs)
                log("<- input BINDING_REQUEST keycodes=%d -> OK" % len(q.scan_codes))
            return
        if ch == CH_SENSOR:
            if mid == 0x8001:                                           # SENSOR_START_REQUEST
                q = sstartreq.SensorStartRequestMessage(); q.ParseFromString(body)
                rs = sstartresp.SensorStartResponseMessage(); rs.status = 0
                await self.send_msg(ch, 0x8002, rs)
                ev = sevent.SensorEventIndication()
                if q.sensor_type == 13:
                    ev.driving_status.add().status = 0                  # unrestricted
                elif q.sensor_type == 10:
                    ev.night_mode.add().is_night = self.night
                else:
                    log("<- sensor start for unsupported type", q.sensor_type); return
                await self.send_msg(ch, 0x8003, ev)
                log("<- sensor START type=%d -> OK + initial event" % q.sensor_type)
            return
        if ch in (CH_VIDEO, CH_MEDIA, CH_SPEECH, CH_SYSTEM):
            return await self.on_av(ch, name, mid, body)
        if ch == CH_MIC:
            if mid == 0x8005:                                           # AV_INPUT_OPEN_REQUEST
                q = avinopen.AVInputOpenRequest(); q.ParseFromString(body)
                rs = avinopenresp.AVInputOpenResponse(); rs.session = 0; rs.value = 0
                await self.send_msg(ch, 0x8006, rs)
                log("<- mic OPEN open=%s -> OK" % q.open)
                self.mic_open = bool(q.open)
                if self.on_mic:
                    self.on_mic(self.mic_open)
            elif mid == 0x8000:
                rs = avsetupresp.AVChannelSetupResponse(); rs.media_status = 2; rs.max_unacked = 1; rs.configs.append(0)
                await self.send_msg(ch, 0x8003, rs)
                log("<- mic SETUP -> OK")
            return
        log("<- %s msg 0x%04x (%d bytes) unhandled" % (name, mid, len(body)))

    async def on_control(self, mid, body):
        if mid == 5:                                                    # SERVICE_DISCOVERY_REQUEST
            log("<- SERVICE_DISCOVERY_REQUEST (%d bytes)" % len(body))
            self.phone_model = discovery_model(body)
            await self.send_msg(CH_CONTROL, 6, self.build_discovery())
            log("-> SERVICE_DISCOVERY_RESPONSE (video res=%d/30 frame=%dx%d ui=%dx%d insets=%s dpi=%d, 3 audio, mic, input, sensors)" % (self.mode["res"], self.mode["fw"], self.mode["fh"], self.mode["uw"], self.mode["uh"], self.mode["insets"] or "-", self.mode["dpi"]))
        elif mid == 0x0b:                                               # PING_REQUEST
            q = pingreq.PingRequest(); q.ParseFromString(body)
            rs = pingresp.PingResponse(); rs.timestamp = q.timestamp
            await self.send_msg(CH_CONTROL, 0x0c, rs, encrypted=False)
        elif mid == 0x0c:
            pass
        elif mid == 0x12:                                               # AUDIO_FOCUS_REQUEST
            q = afreq.AudioFocusRequest(); q.ParseFromString(body)
            rs = afresp.AudioFocusResponse()
            rs.audio_focus_state = 4 if False else (1 if q.audio_focus_type in (1, 2, 3) else 3)   # GAIN / LOSS(release)
            await self.send_msg(CH_CONTROL, 0x13, rs)
            log("<- AUDIO_FOCUS_REQUEST type=%d -> state=%d" % (q.audio_focus_type, rs.audio_focus_state))
            if self.on_focus:
                self.on_focus("audio", q.audio_focus_type)
        elif mid == 0x0d:                                               # NAVIGATION_FOCUS_REQUEST
            rs = navresp.NavigationFocusResponse(); rs.type = 2
            await self.send_msg(CH_CONTROL, 0x0e, rs)
            log("<- NAVIGATION_FOCUS_REQUEST -> granted")
            if self.on_focus:
                self.on_focus("navigation", dict(pb_fields(body)).get(1))
        elif mid == 0x0f:                                               # SHUTDOWN_REQUEST
            await self.send_msg(CH_CONTROL, 0x10, shutresp.ShutdownResponse())
            log("<- SHUTDOWN_REQUEST: closing")
            self.alive = False
        else:
            log("<- control msg 0x%04x (%d bytes) unhandled" % (mid, len(body)))

    async def on_av(self, ch, name, mid, body):
        if mid == 0x8000:                                               # SETUP_REQUEST
            q = avsetupreq.AVChannelSetupRequest(); q.ParseFromString(body)
            rs = avsetupresp.AVChannelSetupResponse(); rs.media_status = 2; rs.max_unacked = 1; rs.configs.append(0)
            await self.send_msg(ch, 0x8003, rs)
            log("<- %s SETUP config=%d -> OK" % (name, q.config_index))
            if ch == CH_VIDEO:                                          # tell the phone it may project (checklist step 12)
                fi = vfi.VideoFocusIndication(); fi.focus_mode = 1; fi.unrequested = True
                await self.send_msg(ch, 0x8008, fi)
                log("-> video FOCUS_INDICATION projected (unsolicited)")
        elif mid == 0x8007:                                             # VIDEO_FOCUS_REQUEST
            q = vfr.VideoFocusRequest(); q.ParseFromString(body)
            rs = vfi.VideoFocusIndication(); rs.focus_mode = 1; rs.unrequested = False
            await self.send_msg(ch, 0x8008, rs)
            log("<- video FOCUS_REQUEST mode=%d -> FOCUSED" % q.focus_mode)
        elif mid == 0x8001:                                             # START_INDICATION
            q = avstart.AVChannelStartIndication(); q.ParseFromString(body)
            self.sessions[ch] = q.session
            log("<- %s START session=%d config=%d" % (name, q.session, q.config))
        elif mid == 0x8002:
            log("<- %s STOP" % name)
        elif mid in (0x0000, 0x0001):                                   # media (with / without timestamp)
            ts, data = (struct.unpack(">Q", body[:8])[0], body[8:]) if mid == 0 else (0, body)
            ack = avack.AVMediaAckIndication(); ack.session = self.sessions.get(ch, 0); ack.value = 1
            await self.send_msg(ch, 0x8004, ack)
            if ch == CH_VIDEO:
                self.stats["video"] += 1; self.stats["video_bytes"] += len(data)
                if self.on_video:
                    self.on_video(ts, data)
            else:
                self.stats["audio"] += 1
                if self.on_audio:
                    self.on_audio(ch, ts, data)
            if time.time() - self._last_stats > 5:
                self._last_stats = time.time()
                log("stats", self.stats)
        else:
            log("<- %s msg 0x%04x (%d bytes) unhandled" % (name, mid, len(body)))


async def handle_client(reader, writer, **kw):
    peer = writer.get_extra_info("peername")
    log("AA session from", peer)
    s = AASession(reader, writer, **kw)
    await s.run()
    return s
