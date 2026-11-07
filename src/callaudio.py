"""Phone-call audio relay (Bluetooth side, runs inside aa_bt.py).

The phone treats the Beagle as a car kit (HSP headset). During a call it opens a Bluetooth SCO audio link to us
(8 kHz, 16-bit, mono, CVSD). This module:
  * keeps the headset RFCOMM link alive (answers "OK" to AT commands),
  * accepts the SCO link, forwards the caller's voice to the bridge (standalone.py) over local UDP, and
  * writes the car's microphone (sent back by the bridge over UDP) into the SCO link, so the other side hears the car mic.

UDP protocol, 127.0.0.1 only:   aa_bt -> bridge (port CALL_TO_BRIDGE):  b"S" call audio started | b"E" ended | b"A"+PCM 8 kHz s16
                                 bridge -> aa_bt (port CALL_FROM_BRIDGE): raw PCM 8 kHz s16 mono (car microphone)
                                 bridge -> aa_bt (port CALL_CTL): b"CKPD" press the headset button (answers, or hangs up)
Off unless AA_CALLS=1.
"""
import array
import ctypes
import ctypes.util
import json
import math
import os
import socket
import threading
import time

ENABLED = os.environ.get("AA_CALLS", "0") == "1"
CALL_TO_BRIDGE = int(os.environ.get("AA_CALL_PORT_OUT", "5290"))
CALL_FROM_BRIDGE = int(os.environ.get("AA_CALL_PORT_IN", "5291"))
CALL_CTL = int(os.environ.get("AA_CALL_PORT_CTL", "5292"))
SCO_BATCH = 320                  # bytes (20 ms at 8 kHz) per UDP datagram; SCO packets are small (typ. 48 bytes)
MIC_CAP = 8000 * 2 // 2          # keep at most 0.5 s of car microphone audio
MIC_GAIN = float(os.environ.get("AA_CALL_MIC_GAIN", "6"))     # the car mic arrives quiet; phone apps can mistake it for noise
DUCK = float(os.environ.get("AA_CALL_DUCK", "0.1"))           # mic level while the caller is talking (1 = no echo suppression)
DUCK_LEVEL = int(os.environ.get("AA_CALL_DUCK_LEVEL", "300"))  # caller sample above this counts as talking
DUCK_HOLD = float(os.environ.get("AA_CALL_DUCK_HOLD", "0.4"))  # seconds to stay ducked: the speaker-to-mic echo arrives late
ECHO_MARGIN = float(os.environ.get("AA_CALL_ECHO_MARGIN", "0.3"))   # stay down this long past the measured echo delay
ECHO_HOLD_MAX = float(os.environ.get("AA_CALL_ECHO_HOLD_MAX", "2.5"))
AEC = os.environ.get("AA_CALL_AEC", "1") == "1"                 # echo canceller (speexdsp) instead of turning the mic down
AEC_TAIL = float(os.environ.get("AA_CALL_AEC_TAIL", "0.2"))     # seconds of echo the canceller models, around the measured delay
AEC_LEAD = float(os.environ.get("AA_CALL_AEC_LEAD", "0.05"))    # how much earlier than the measured delay that window starts
AEC_DELAY = float(os.environ.get("AA_CALL_ECHO_DELAY", "0.62")) # starting delay until one is measured (the IM L6's, 620 ms)
AEC_SUPPRESS = int(os.environ.get("AA_CALL_AEC_SUPPRESS", "-40"))   # dB of leftover echo removed after cancelling
AEC_DENOISE = os.environ.get("AA_CALL_AEC_DENOISE", "1") == "1"  # speex only applies the echo suppression with this on
AEC_DUCK = float(os.environ.get("AA_CALL_AEC_DUCK", "1"))       # also turn the mic down while the caller talks (1 = no)
AEC_WARMUP = float(os.environ.get("AA_CALL_AEC_WARMUP", "2"))  # seconds the canceller needs to learn; the mic is turned down meanwhile
ECHO_FILE = os.environ.get("AA_CALL_ECHO_FILE", "/var/lib/beagle-aa-bridge/echo.json")   # last measured delay, for the next call
TONE_FILE ="/run/aa-call-tone"  # if this file exists when a call starts, send a 440 Hz test tone instead of the car mic
TONE = array.array("h", [int(12000 * math.sin(2 * math.pi * 440 * i / 8000)) for i in range(8000)]).tobytes()   # 1 s, loops cleanly


def peak(pcm):
    """Loudest sample in a block of 16-bit PCM (0 = digital silence, 32767 = full scale)."""
    a = array.array("h")
    a.frombytes(pcm[:len(pcm) // 2 * 2])
    return max(max(a), -min(a)) if a else 0


def amplify(pcm, gain):
    """Scale 16-bit PCM, clipping at full scale."""
    a = array.array("h")
    a.frombytes(pcm[:len(pcm) // 2 * 2])
    return array.array("h", [max(-32768, min(32767, int(x * gain))) for x in a]).tobytes()


class Ducker:
    """Half-duplex echo suppression: the car mic hears the caller from the car speakers, so turn it down while they talk."""

    def __init__(self, depth=DUCK, level=DUCK_LEVEL, hold=DUCK_HOLD):
        self.depth, self.level, self.hold = depth, level, hold
        self.gain = 1.0
        self.until = 0.0
        self.ducked = 0                                        # packets spent turned down, for the log

    def process(self, caller_peak, mic, now):
        if caller_peak >= self.level:
            self.until = now + self.hold
        target = self.depth if now < self.until else 1.0
        self.gain += (target - self.gain) * (0.5 if target < self.gain else 0.05)    # fast down, slow up: no clicks
        if target < 1.0:
            self.ducked += 1
        return mic if self.gain > 0.99 else amplify(mic, self.gain)


def _speex():
    """libspeexdsp through ctypes, or None if it is not installed (apt install libspeexdsp1)."""
    try:
        lib = ctypes.CDLL(ctypes.util.find_library("speexdsp") or "libspeexdsp.so.1")
    except OSError:
        return None
    vp, i16p = ctypes.c_void_p, ctypes.POINTER(ctypes.c_int16)
    lib.speex_echo_state_init.restype = vp
    lib.speex_echo_state_init.argtypes = [ctypes.c_int, ctypes.c_int]
    lib.speex_echo_state_destroy.argtypes = [vp]
    lib.speex_echo_cancellation.argtypes = [vp, i16p, i16p, i16p]
    lib.speex_echo_ctl.argtypes = [vp, ctypes.c_int, vp]
    lib.speex_preprocess_state_init.restype = vp
    lib.speex_preprocess_state_init.argtypes = [ctypes.c_int, ctypes.c_int]
    lib.speex_preprocess_state_destroy.argtypes = [vp]
    lib.speex_preprocess_run.argtypes = [vp, i16p]
    lib.speex_preprocess_ctl.argtypes = [vp, ctypes.c_int, vp]
    return lib


SPEEX = _speex() if AEC else None
SPEEX_ECHO_SET_SAMPLING_RATE, PP_SET_DENOISE, PP_SET_ECHO_SUPPRESS, PP_SET_ECHO_SUPPRESS_ACTIVE, PP_SET_ECHO_STATE = 24, 0, 20, 22, 24


def saved_delay():
    try:
        with open(ECHO_FILE) as f:
            d = float(json.load(f)["delay"])
        return d if 0 <= d <= 2.0 else AEC_DELAY
    except (OSError, ValueError, KeyError, TypeError):
        return AEC_DELAY


def save_delay(d):
    try:
        os.makedirs(os.path.dirname(ECHO_FILE), exist_ok=True)
        with open(ECHO_FILE + ".tmp", "w") as f:
            json.dump({"delay": round(d, 3)}, f)
        os.replace(ECHO_FILE + ".tmp", ECHO_FILE)
    except OSError:
        pass


class Canceller:
    """Echo canceller (speexdsp): learns how the caller's voice comes back through the car speaker and mic and subtracts it, so
    both sides can talk at once. The echo comes back late (the car buffers), so the caller's voice is delayed by about that much
    before it is used as the reference, and the canceller only has to model the window around it.

    Packets arrive in lockstep: for every packet of caller audio one packet of the same length goes back, so caller sample i and
    mic sample i are the same moment. The mic goes out one 20 ms frame late."""
    FRAME = 160                                                # samples (20 ms)

    def __init__(self, delay, gain=MIC_GAIN, tail=AEC_TAIL, lead=AEC_LEAD):
        self.gain = gain
        self.delay = delay
        self.tail = tail
        self.shift = int(max(0.0, delay - lead) * 8000)        # samples the reference is held back
        self.ref = bytearray()                                 # caller samples from (pos - shift) on
        self.ref_start = -self.shift                           # sample index of ref[0]
        self.pos = 0                                           # mic samples processed
        self.mic = bytearray()
        self.out = bytearray(b"\0" * self.FRAME * 2)          # one frame of slack: never runs dry
        self.st = SPEEX.speex_echo_state_init(self.FRAME, int(tail * 8000))
        self.pp = SPEEX.speex_preprocess_state_init(self.FRAME, 8000)
        rate = ctypes.c_int(8000)
        SPEEX.speex_echo_ctl(self.st, SPEEX_ECHO_SET_SAMPLING_RATE, ctypes.byref(rate))
        SPEEX.speex_preprocess_ctl(self.pp, PP_SET_ECHO_STATE, ctypes.c_void_p(self.st))
        for req, v in ((PP_SET_ECHO_SUPPRESS, AEC_SUPPRESS), (PP_SET_ECHO_SUPPRESS_ACTIVE, AEC_SUPPRESS // 2 - 5),
                       (PP_SET_DENOISE, int(AEC_DENOISE))):
            x = ctypes.c_int(v)
            SPEEX.speex_preprocess_ctl(self.pp, req, ctypes.byref(x))
        self.frames = 0
        if self.shift:
            self.ref += b"\0" * (self.shift * 2)              # before the call started the caller was silent

    def covers(self, delay):
        """True if an echo after `delay` seconds falls inside the window this canceller models (room for reflections after it)."""
        start = self.shift / 8000
        return start <= delay <= start + self.tail - 0.08

    def close(self):
        if self.st:
            SPEEX.speex_preprocess_state_destroy(self.pp)
            SPEEX.speex_echo_state_destroy(self.st)
            self.st = self.pp = None

    def process(self, caller, mic):
        """Caller packet in, mic packet of the same length out."""
        self.ref += caller
        self.mic += mic
        F, B = self.FRAME, self.FRAME * 2
        while len(self.mic) >= B:
            i = (self.pos - self.shift - self.ref_start) * 2   # this frame's reference: caller audio `shift` samples ago
            ref = bytes(self.ref[i:i + B])
            if len(ref) < B:
                ref += b"\0" * (B - len(ref))
            rec = (ctypes.c_int16 * F).from_buffer_copy(bytes(self.mic[:B]))
            play = (ctypes.c_int16 * F).from_buffer_copy(ref)
            res = (ctypes.c_int16 * F)()
            SPEEX.speex_echo_cancellation(self.st, rec, play, res)
            SPEEX.speex_preprocess_run(self.pp, res)
            frame = bytes(res)
            self.out += frame if self.gain == 1 else amplify(frame, self.gain)
            del self.mic[:B]
            self.pos += F
            self.frames += 1
            drop = (self.pos - self.shift - self.ref_start) * 2    # reference before the next frame's is no longer needed
            if drop > 0:
                del self.ref[:drop]
                self.ref_start = self.pos - self.shift
        n = len(mic)
        if len(self.out) < n:                                  # should not happen: the slack frame covers it
            self.out += b"\0" * (n - len(self.out))
        d = bytes(self.out[:n])
        del self.out[:n]
        return d


class EchoMeter:
    """Finds how late the caller's voice comes back through the car mic (car speaker -> mic, plus the car's buffering), so the
    ducker can stay down for that long. Works on 20 ms loudness blocks: the caller's loudness, shifted by the right delay,
    matches the mic's loudness when the mic hears mostly echo."""
    BLOCK = 160                                      # samples per loudness block (20 ms at 8 kHz)
    MAX_LAG = int(os.environ.get("AA_CALL_ECHO_MAX", "100"))       # blocks: 2 s
    WINDOW = 250                                     # blocks looked at (5 s)
    MIN_CORR = 0.5                                   # how well the shifted loudness has to match to trust the delay

    def __init__(self):
        self.blocks = []                             # (caller loudness, mic loudness) per block; one list, so a copy is consistent
        self._c = self._m = self._n = 0
        self.count = 0                               # blocks seen
        self.lag = None                              # blocks
        self.corr = 0.0

    def add(self, caller_pcm, mic_pcm):
        a, b = array.array("h"), array.array("h")
        a.frombytes(caller_pcm[:len(caller_pcm) // 2 * 2])
        b.frombytes(mic_pcm[:len(mic_pcm) // 2 * 2])
        self._c += sum(abs(x) for x in a)
        self._m += sum(abs(x) for x in b)
        self._n += min(len(a), len(b)) or max(len(a), len(b))
        if self._n >= self.BLOCK:
            self.blocks.append((self._c / self._n, self._m / self._n))
            self._c = self._m = self._n = 0
            self.count += 1
            if len(self.blocks) > self.WINDOW + self.MAX_LAG:
                del self.blocks[0]
            return True
        return False

    def estimate(self):
        """Best delay in seconds, or None until the caller has talked enough to tell."""
        b = list(self.blocks)                        # the audio thread keeps adding while this runs
        c, m = [x for x, _ in b], [y for _, y in b]
        n = len(m) - self.MAX_LAG
        if n < 50:
            return None
        best, best_lag = 0.0, None
        mm = m[-n:]
        mean_m = sum(mm) / n
        dm = [x - mean_m for x in mm]
        sm = math.sqrt(sum(x * x for x in dm))
        for lag in range(0, self.MAX_LAG + 1):
            cc = c[len(c) - n - lag:len(c) - lag]
            mean_c = sum(cc) / n
            dc = [x - mean_c for x in cc]
            sc = math.sqrt(sum(x * x for x in dc))
            if sc == 0 or sm == 0:
                continue
            r = sum(x * y for x, y in zip(dc, dm)) / (sc * sm)
            if r > best:
                best, best_lag = r, lag
        self.corr = best
        if best >= self.MIN_CORR:
            self.lag = best_lag
        return None if self.lag is None else self.lag * self.BLOCK / 8000


class CallAudio:
    def __init__(self, log):
        self.log = log
        self.mic = bytearray()
        self.mic_lock = threading.Lock()
        self.out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.active = False
        self.fill = 0
        self.rfcomm = None
        self.sco_listener = None

    # -------------------------------------------------------------- headset RFCOMM link (HSP)
    def attach_rfcomm(self, sock):
        threading.Thread(target=self._rfcomm, args=(sock,), daemon=True).start()

    def press_button(self):
        """HSP headset button: the phone answers a ringing call, or ends the current one."""
        if not self.rfcomm:
            self.log("headset button: no headset link")
            return
        try:
            self.rfcomm.sendall(b"AT+CKPD=200\r")
            self.log("headset link -> AT+CKPD=200")
        except OSError as e:
            self.log("headset button failed: %r" % e)

    def _ctl_receiver(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", CALL_CTL))
        while True:
            d, _ = s.recvfrom(64)
            if d == b"CKPD":
                self.press_button()

    def _rfcomm(self, sock):
        self.rfcomm = sock
        sock.settimeout(None)
        buf = b""
        try:
            while True:
                d = sock.recv(256)
                if not d:
                    break
                buf += d
                while b"\r" in buf:
                    line, buf = buf.split(b"\r", 1)
                    line = line.strip()
                    if line:
                        self.log("headset link <- %r" % line)
                        if line.upper().startswith(b"AT"):
                            sock.sendall(b"\r\nOK\r\n")
        except OSError as e:
            self.log("headset link closed: %r" % e)
        finally:
            if self.rfcomm is sock:
                self.rfcomm = None                             # aa_bt.py reopens it while Android Auto is running

    # -------------------------------------------------------------- SCO audio
    def start(self, adapter_addr):
        """Listen for the phone's SCO connection on the local adapter, and for the car microphone from the bridge."""
        threading.Thread(target=self._mic_receiver, daemon=True).start()
        threading.Thread(target=self._ctl_receiver, daemon=True).start()
        try:
            ls = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_SEQPACKET, socket.BTPROTO_SCO)
            ls.bind(adapter_addr)
            ls.listen(1)
        except OSError as e:
            self.log("call audio: cannot listen for SCO: %r" % e)
            return
        self.sco_listener = ls
        self.log("call audio: waiting for the phone's SCO audio link on %s" % adapter_addr)
        threading.Thread(target=self._accept_loop, args=(ls,), daemon=True).start()

    def _accept_loop(self, ls):
        while True:
            try:
                conn, peer = ls.accept()
            except OSError as e:
                self.log("call audio: accept failed: %r" % e)
                time.sleep(2)
                continue
            self.log("call audio: SCO link from %s" % (peer,))
            self.relay(conn)

    def _mic_receiver(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", CALL_FROM_BRIDGE))
        while True:
            d, _ = s.recvfrom(4096)
            if MIC_GAIN != 1 and not SPEEX:                    # with the canceller the gain goes on after it: clipped echo cannot be cancelled
                d = amplify(d, MIC_GAIN)
            with self.mic_lock:
                self.mic += d
                if len(self.mic) > MIC_CAP:
                    del self.mic[:len(self.mic) - MIC_CAP]

    def _take_mic(self, n):
        with self.mic_lock:
            if len(self.mic) >= n:
                d = bytes(self.mic[:n])
                del self.mic[:n]
                return d
        self.fill += 1
        return b"\0" * n                                       # silence when the car mic has nothing yet

    def _measure(self, meter, ducker):
        delay = meter.estimate()
        if delay is not None:
            prev, self.last_estimate = self.last_estimate, delay
            if prev is not None and abs(prev - delay) <= 0.025:    # two estimates in a row agree: the delay is real, not noise
                self.measured = delay
            hold = min(ECHO_HOLD_MAX, max(DUCK_HOLD, delay + ECHO_MARGIN))
            if abs(hold - ducker.hold) > 0.05:
                self.log("call audio: echo comes back after %.0f ms (match %.2f), mic stays down %.2f s" % (delay * 1000, meter.corr, hold))
                ducker.hold = hold

    def relay(self, conn):
        """Move audio both ways on one SCO link until it closes (call over)."""
        self.active = True
        with self.mic_lock:
            self.mic.clear()
        self.out.sendto(b"S", ("127.0.0.1", CALL_TO_BRIDGE))
        pending = bytearray()
        n_in = n_out = peak_in = peak_out = tone_pos = self.fill = 0
        tone = os.path.exists(TONE_FILE)
        meter = EchoMeter()
        busy = None
        self.measured = self.last_estimate = None
        aec = Canceller(saved_delay()) if SPEEX and not tone else None
        ducker = Ducker(depth=AEC_DUCK) if aec else Ducker()
        warm = Ducker()                                        # while the canceller is still learning
        if aec:
            warm.hold = min(ECHO_HOLD_MAX, max(DUCK_HOLD, aec.delay + ECHO_MARGIN))
        if tone:
            self.log("call audio: sending the 440 Hz test tone instead of the car mic")
        elif aec:
            self.log("call audio: echo canceller on, echo expected after %.0f ms" % (aec.delay * 1000))
        else:
            self.log("call audio: no echo canceller (%s), turning the mic down while the caller talks"
                     % ("AA_CALL_AEC=0" if not AEC else "libspeexdsp1 not installed"))
        t0 = time.time()
        try:
            while True:
                d = conn.recv(1024)
                if not d:
                    break
                n_in += 1
                pending += d
                if len(pending) >= SCO_BATCH:
                    self.out.sendto(b"A" + bytes(pending), ("127.0.0.1", CALL_TO_BRIDGE))
                    pending.clear()
                if tone:
                    m = (TONE + TONE)[tone_pos:tone_pos + len(d)]
                    tone_pos = (tone_pos + len(d)) % len(TONE)
                else:
                    raw = self._take_mic(len(d))                  # every packet in -> one packet of car mic out
                    if meter.add(d, raw) and meter.count % 50 == 0 and not (busy and busy.is_alive()):
                        busy = threading.Thread(target=self._measure, args=(meter, ducker), daemon=True)   # off the audio path
                        busy.start()
                    if aec:
                        if self.measured is not None and not aec.covers(self.measured):
                            self.log("call audio: echo canceller re-aligned to %.0f ms (was %.0f)" % (self.measured * 1000, aec.delay * 1000))
                            aec.close()
                            aec = Canceller(self.measured)
                            warm.hold = min(ECHO_HOLD_MAX, max(DUCK_HOLD, aec.delay + ECHO_MARGIN))
                        m = aec.process(d, raw)
                        if aec.frames < AEC_WARMUP * 50:
                            m = warm.process(peak(d), m, time.time())
                        elif AEC_DUCK < 1:
                            m = ducker.process(peak(d), m, time.time())
                    else:
                        m = ducker.process(peak(d), raw, time.time())
                conn.send(m)
                n_out += 1
                peak_in, peak_out = max(peak_in, peak(d)), max(peak_out, peak(m))
        except OSError as e:
            self.log("call audio: SCO link ended: %r" % e)
        finally:
            self.active = False
            try:
                conn.close()
            except OSError:
                pass
            self.out.sendto(b"E", ("127.0.0.1", CALL_TO_BRIDGE))
            self.log("call audio: SCO closed after %.0f s (%d packets in, %d out, %d of them empty filler, %d ducked; loudest sample: caller %d, sent to phone %d of 32767, gain %g)"
                     % (time.time() - t0, n_in, n_out, self.fill, ducker.ducked, peak_in, peak_out, MIC_GAIN))
            self.log("call audio: echo delay %s (best match %.2f), %s"
                     % ("%.0f ms" % (meter.lag * meter.BLOCK / 8) if meter.lag is not None else "not found", meter.corr,
                        "echo canceller, %d frames, aligned to %.0f ms" % (aec.frames, aec.delay * 1000) if aec else
                        "mic hold %.2f s, depth %g" % (ducker.hold, ducker.depth)))
            if meter.lag is not None:
                save_delay(meter.lag * meter.BLOCK / 8000)
            if aec:
                aec.close()
