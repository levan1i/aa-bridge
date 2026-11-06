"""Which phone the car is told is connected.

data/session_ref.json holds placeholders in place of the phone in the capture, so nothing personal is in the repo. aa_bt.py saves
the Bluetooth address and name of the phone that starts Android Auto, and the placeholders are filled in as the frames are sent.
Until a phone has been seen, or with AA_PHONE_ID=0, the car gets the placeholders.
"""
import json
import os
import re

ENABLED = os.environ.get("AA_PHONE_ID", "1") == "1"
PATH = os.environ.get("AA_PHONE_FILE", "/var/lib/beagle-aa-bridge/phone.json")
MAC, NAME, MODEL = b"02:00:00:00:00:01", b"My Android", b"Android Phone  "
T_BOXSET = 0x19


def save(mac, name):
    os.makedirs(os.path.dirname(PATH), exist_ok=True)
    tmp = PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"mac": mac, "name": name}, f, ensure_ascii=False)
    os.replace(tmp, PATH)


def load():
    """The last phone seen, as {"mac", "name"}; empty if there is none or the feature is off."""
    if not ENABLED:
        return {}
    try:
        with open(PATH) as f:
            p = json.load(f)
        return p if re.fullmatch(r"([0-9A-F]{2}:){5}[0-9A-F]{2}", p["mac"]) and isinstance(p["name"], str) else {}
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def fill(mtype, body, phone, model=""):
    """A frame from session_ref.json with the placeholders replaced. BoxSettings is JSON, so its text is escaped."""
    def text(s):
        s = " ".join(s.split())
        return (json.dumps(s, ensure_ascii=False)[1:-1] if mtype == T_BOXSET else s).encode()
    if phone:
        body = body.replace(MAC, phone["mac"].encode())
        if phone["name"].strip():
            body = body.replace(NAME, text(phone["name"]))
    if model and ENABLED:
        body = body.replace(MODEL, text(model))
    return body
