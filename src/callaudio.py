"""Phone-call audio relay (Bluetooth side, runs inside aa_bt.py).

The phone treats the Beagle as a car kit (HSP headset). During a call it opens a Bluetooth SCO audio link to us
(8 kHz, 16-bit, mono, CVSD). This module:
  * keeps the headset RFCOMM link alive (answers "OK" to AT commands),
  * accepts the SCO link, forwards the caller's voice to the bridge (standalone.py) over local UDP, and
  * writes the car's microphone (sent back by the bridge over UDP) into the SCO link, so the other side hears the car mic.

UDP protocol, 127.0.0.1 only:   aa_bt -> bridge (port CALL_TO_BRIDGE):  b"S" call audio started | b"E" ended | b"A"+PCM 8 kHz s16
                                 bridge -> aa_bt (port CALL_FROM_BRIDGE): raw PCM 8 kHz s16 mono (car microphone)
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
SCO_BATCH = 320                  # bytes (20 ms at 8 kHz) per UDP datagram; SCO packets are small (typ. 48 bytes)
MIC_CAP = 8000 * 2 // 2          # keep at most 0.5 s of car microphone audio
MIC_GAIN = float(os.environ.get("AA_CALL_MIC_GAIN", "6"))     # the car mic arrives quiet; phone apps can mistake it for noise
TONE_FILE = "/run/aa-call-tone"  # if this file exists when a call starts, send a 440 Hz test tone instead of the car mic
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


class CallAudio:
    def __init__(self, log):
        self.log = log
        self.mic = bytearray()
        self.mic_lock = threading.Lock()
        self.out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.active = False
        self.fill = 0
        self.sco_listener = None

    # -------------------------------------------------------------- headset RFCOMM link (HSP)
    def attach_rfcomm(self, sock):
        threading.Thread(target=self._rfcomm, args=(sock,), daemon=True).start()

    def _rfcomm(self, sock):
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

    def relay(self, conn):
        """Move audio both ways on one SCO link until it closes (call over)."""
        self.active = True
        with self.mic_lock:
            self.mic.clear()
        self.out.sendto(b"S", ("127.0.0.1", CALL_TO_BRIDGE))
        pending = bytearray()
        n_in = n_out = peak_in = peak_out = tone_pos = self.fill = 0
        tone = os.path.exists(TONE_FILE)
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
                    m = self._take_mic(len(d))                 # every packet in -> one packet of car microphone out
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
            self.log("call audio: SCO closed after %.0f s (%d packets in, %d out, %d of them empty filler; loudest sample: caller %d, sent to phone %d of 32767, gain %g)"
                     % (time.time() - t0, n_in, n_out, self.fill, peak_in, peak_out, MIC_GAIN))
