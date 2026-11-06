#!/usr/bin/env python3
"""Send one test frame to the car through the running bridge, to see what the car does with it. Needs AA_TEST=1 in the bridge.

Each command sends a single frame, so a reaction can be pinned on it. Replies from the car are in /var/log/standalone.log.

  carpoke.py song "Title" "Artist" "Album" [seconds]   track details          carpoke.py cmd 502               Command N
  carpoke.py time 42                                   position in seconds    carpoke.py audio 12 [decode] [audiotype]   audio command byte
  carpoke.py status 1                                  MediaPlayStatus        carpoke.py phase 8               Phase N
  carpoke.py app "Name"                                MediaAPPName           carpoke.py plugged 3 [wireless]  Plugged: 3 CarPlay, 5 Android Auto, 6 HiCar
  carpoke.py cover                                     small test cover       carpoke.py unplugged
  carpoke.py call 2 "Name" "Number" [direction]        caller details         carpoke.py log                   ask the car app for its log (161)
  carpoke.py nav "Road" [metres] [order] [side]        turn information       carpoke.py raw TYPE HEX [plain]  any frame
  carpoke.py navstatus 1                               navigation on (2 off)
"""
import json
import os
import socket
import struct
import sys
import zlib

PORT = int(os.environ.get("AA_TEST_PORT", "5293"))
T_PLUGGED, T_PHASE, T_UNPLUGGED, T_AUDIO, T_CMD, T_META, T_LOG = 2, 3, 4, 7, 8, 0x2A, 0xA1


def meta(subtype, info):
    return T_META, 1, struct.pack("<I", subtype) + json.dumps(info, ensure_ascii=False, separators=(",", ":")).encode() + b"\0"


def png(size=64, rgb=(220, 40, 40)):
    def chunk(name, data):
        return struct.pack(">I", len(data)) + name + data + struct.pack(">I", zlib.crc32(name + data))
    rows = (b"\0" + bytes(rgb) * size) * size
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")


def frame(a):
    """Command line -> (message type, plain, payload)."""
    op, v = a[0], a[1:]
    if op == "song":
        info = {"MediaSongName": v[0], "MediaArtistName": v[1], "MediaAlbumName": v[2]}
        if len(v) > 3:
            info["MediaSongDuration"] = int(v[3]) * 1000
        return meta(1, info)
    if op == "time":
        return meta(1, {"MediaSongPlayTime": int(v[0]) * 1000})
    if op == "status":
        return meta(1, {"MediaPlayStatus": int(v[0])})
    if op == "app":
        return meta(1, {"MediaAPPName": v[0]})
    if op == "cover":
        return T_META, 1, struct.pack("<I", 2) + png()
    if op == "call":
        return meta(100, {"CallStatus": int(v[0]), "CallDirection": int(v[3]) if len(v) > 3 else 1, "CallNumber": v[2], "CallID": v[2], "CallName": v[1]})
    if op == "nav":
        info = {"NaviRoadName": v[0], "NaviOrderType": int(v[2]) if len(v) > 2 else 6, "NaviTurnSide": int(v[3]) if len(v) > 3 else 2,
                "NaviTurnAngle": 2, "NaviRoundaboutExit": 0, "NaviRemainDistance": int(v[1]) if len(v) > 1 else 300}
        return meta(200, info)
    if op == "navstatus":
        return meta(200, {"NaviStatus": int(v[0])})
    if op == "cmd":
        return T_CMD, 0, struct.pack("<I", int(v[0]))
    if op == "audio":
        return T_AUDIO, 0, struct.pack("<IfI", int(v[1]) if len(v) > 1 else 4, 0.0, int(v[2]) if len(v) > 2 else 1) + bytes([int(v[0])])
    if op == "phase":
        return T_PHASE, 0, struct.pack("<I", int(v[0]))
    if op == "plugged":
        return T_PLUGGED, 0, struct.pack("<II", int(v[0]), int(v[1]) if len(v) > 1 else 1)
    if op == "unplugged":
        return T_UNPLUGGED, 0, b""
    if op == "log":
        return T_LOG, 0, b""
    if op == "raw":
        return int(v[0], 0), 1 if len(v) > 2 else 0, bytes.fromhex(v[1])
    raise ValueError(op)


def main():
    try:
        mtype, plain, payload = frame(sys.argv[1:])
    except (IndexError, ValueError):
        sys.exit(__doc__)
    socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(struct.pack("<IB", mtype, plain) + payload, ("127.0.0.1", PORT))
    print("sent type 0x%x, %d bytes" % (mtype, len(payload)))


if __name__ == "__main__":
    main()
