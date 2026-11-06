#!/usr/bin/env python3
"""Make the still picture the car shows while no phone is connected: data/connecting-<W>x<H>.h264 for a car display of W x H.

The car shows the top-left W x H of the video frame (see compute_mode in src/aa_session.py), so the picture is drawn there.
Needs Pillow and ffmpeg with libx264; run it on a PC and commit the result.   usage: make_connecting.py [W H]
"""
import os, subprocess, sys
from PIL import Image, ImageDraw, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TIERS = {1: (800, 480), 2: (1280, 720), 3: (1920, 1080)}
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"


def frame_size(w, h):
    return TIERS[1 if w <= 800 else 2 if w <= 1280 else 3]


def picture(w, h):
    fw, fh = frame_size(w, h)
    img = Image.new("RGB", (fw, fh), (12, 14, 18))
    d = ImageDraw.Draw(img)
    size = h // 12
    title, sub = ImageFont.truetype(FONT, size), ImageFont.truetype(FONT, size // 2)
    cx, cy = w // 2, h // 2
    d.text((cx, cy - size // 2), "Connecting to your phone", font=title, fill=(235, 238, 242), anchor="mm")
    d.text((cx, cy + size), "Android Auto starts by itself when the phone connects", font=sub, fill=(140, 148, 160), anchor="mm")
    return img


def main():
    w, h = (int(sys.argv[1]), int(sys.argv[2])) if len(sys.argv) == 3 else (1920, 720)
    img = picture(w, h)
    out = os.path.join(ROOT, "data", "connecting-%dx%d.h264" % (w, h))
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", "%dx%d" % img.size, "-i", "-",
                    "-frames:v", "1", "-c:v", "libx264", "-profile:v", "baseline", "-level", "4.0", "-pix_fmt", "yuv420p",
                    "-x264-params", "keyint=1:bframes=0:aud=0:repeat-headers=1", "-qp", "22", "-f", "h264", out],
                   input=img.tobytes(), check=True)
    print(out, os.path.getsize(out), "bytes")


if __name__ == "__main__":
    main()
