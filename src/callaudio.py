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
            if MIC_GAIN != 1:
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
        ducker = Ducker()
        meter = EchoMeter()
        busy = None
        if tone:
            self.log("call audio: sending the 440 Hz test tone instead of the car mic")
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
            self.log("call audio: echo delay %s (best match %.2f), mic hold %.2f s, depth %g"
                     % ("%.0f ms" % (meter.lag * meter.BLOCK / 8) if meter.lag is not None else "not found", meter.corr, ducker.hold, ducker.depth))
