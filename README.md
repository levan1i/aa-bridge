# beagle-aa-bridge

A **BeagleY-AI that replaces a Carlinkit-style wireless Android Auto dongle**.

The car's USB-A/USB-C port sees a `1314:1521` "Auto Box" (the Carlinkit CPC200-CCPA / A15W identity the **AutoKit**
app expects). The Beagle itself is the **Android Auto head unit** for the phone, over wireless Android Auto
(Bluetooth hand-off, then Wi-Fi). No dongle hardware is involved.

## The problem it solves

Chinese-built cars that are exported come in different flavours:

* **EU-spec exports** are made for Europe and ship with Android Auto (and CarPlay) built in. No dongle is needed.
* **Chinese-market cars exported with modified software** sit in a middle ground: the software is adapted for export, but
  the head unit does not allow third-party apps, so you cannot simply install an Android Auto or projection app. The one
  this was built for, an IM L6, is of this kind.

On cars of the second kind the only way to get phone projection is the head unit's own **AutoKit** app, which talks to a
small USB "box" (Carlinkit CPC200-CCPA / A15W and clones) that does the phone-projection work.
The box is closed hardware with closed firmware. In the original setup it worked with an iPhone but **rebooted as soon as
an Android phone connected**, and nothing could be done about it: no source, no way to fix or even log what failed.

This project replaces the box with a board you control. The car still believes it has its Carlinkit box, and any phone with
**wireless Android Auto** connects to it as if it were a normal head unit (Bluetooth for the hand-off, Wi-Fi for the stream),
without a cable. Because it is open code, protocol problems can be logged, understood and fixed, and the video size, audio
and touch handling can be adapted to the car.

```
 phone ──Bluetooth (RFCOMM hand-off)──► Beagle (BlueZ + aa_bt.py)
 phone ──Wi-Fi 5 GHz AP "WirelessAADongle"──► Beagle (hostapd, TCP 5288, aa_session.py: Android Auto head unit)
 Beagle ──USB gadget 1314:1521 (FunctionFS, standalone.py + boxd.py: Carlinkit protocol)──► car (AutoKit app)
```

* video: phone H.264 → Carlinkit video messages (no transcoding; the Beagle forwards the phone's frames)
* audio: phone PCM → Carlinkit audio messages · touch: car → Android Auto input events
* resolution follows the car's `Open` request (see [docs/resolution.md](docs/resolution.md))

## Status

Works end to end with the AutoKit app (tested on an IM L6 and with AutoKit on a second phone / in an Android-x86 VM):
Android Auto UI, touch, and audio reach the car, and the picture quality over the 5 GHz link is good. **Not finished**, see [Known gaps](#known-gaps). This is a research
project built from the public aasdk / open-android-auto work plus captures of a real dongle; expect rough edges.

## Hardware

**Everything here was developed and tested on a BeagleY-AI only.** The design is not tied to it: any single-board computer
with the capabilities below should be able to run the bridge, but no other board has been tried, so expect to adapt
board-specific settings (USB controller name, Wi-Fi interface and driver, power). See [docs/boards.md](docs/boards.md).

Required capabilities:

* a USB port that can act as a **device** (OTG/peripheral, FunctionFS/configfs gadget) to plug into the car
* Wi-Fi with **5 GHz and AP mode** (for 1080p), plus classic Bluetooth (BR/EDR, HFP), on board or via USB adapters
* a stable 5 V supply within what the car's USB port can deliver

What was used on the prototype:


| Part | Notes |
|---|---|
| BeagleY-AI | USB-C gadget port (`31000000.usb`) goes to the car |
| USB Bluetooth adapter | needs classic BT; **TP-Link UB500 (RTL8761B)** works. The on-board CC33xx Bluetooth is BLE-only |
| 5 GHz USB Wi-Fi adapter that can do **AP mode** | **TP-Link Archer T2U Nano (RTL8811AU, `rtw88_8821au`)** works. 5 GHz is required for 1080p |
| Power | the board must have a solid 5 V supply; brown-outs look like random reboots |

Why 5 GHz: Android Auto refuses video above 720p on a 2.4 GHz link (`VideoCodecResolutionType 3 is not allowed due
wireless frequency`), and the on-board CC33xx Wi-Fi is 2.4 GHz only. Details in [docs/wifi.md](docs/wifi.md).

## Install (on the Beagle)

```bash
git clone <this repo> && cd beagle-aa-bridge
sudo scripts/install.sh --iface wlan2 --channel 36 --country GE
```

The script installs the packages, generates `gen/` (protobufs) and `certs/` from the aasdk project
(`scripts/fetch-assets.sh`; nothing from aasdk is committed here), creates `/etc/beagle-aa-bridge/env` with a **random
Wi-Fi password**, renders the hostapd/dnsmasq config and enables the services. It prints the SSID and password at the end.

Then: pair the phone with the Bluetooth device **"Beagle Auto"** once, plug the Beagle into the car, open the car app.
The phone shows the Android Auto prompt over Bluetooth and joins the Beagle's Wi-Fi by itself.

Services: `aa-hostapd`, `aa-dnsmasq` (the AP), `aa-bt-agent` + `aa-bt` (Bluetooth hand-off), `standalone` (the bridge).
Logs: `/var/log/standalone.log`, `/var/log/aa_bt.log`.

## Configuration

`/etc/beagle-aa-bridge/env` (read by the services):

| Variable | Default | Meaning |
|---|---|---|
| `AA_SSID`, `AA_KEY` | `WirelessAADongle`, random | AP name / password sent to the phone over Bluetooth (change both together with `hostapd.conf`) |
| `AA_AP_IFACE` | `wlan2` | Wi-Fi interface running the AP; its MAC is the BSSID the phone is told |
| `AA_AP_DRIVER` | `rtw88` | kernel driver of the AP adapter (used by `aa-mkap.sh` / `aa-iwd.sh`) |
| `AA_RES` | follow the car | force an AA video size: 1=800x480, 2=1280x720, 3=1920x1080, 5/6/7 = portrait |
| `AA_DPI` | 160 | density the phone lays its UI out for |
| `AA_NIGHT` | `auto` | day/night theme sent to the phone: `auto` (the car's own report if it sends one, otherwise by clock), or fixed `day` / `night` |
| `AA_NIGHT_FROM`, `AA_NIGHT_TO` | `18`, `7` | hours (local time) when night starts and ends in `auto` mode |
| `AA_CALLS` | `0` | experimental [phone-call audio](docs/calls.md): relay the call voice to the car speakers and the car microphone back to the phone |
| `AA_TZ` | system time zone | time zone for the clock-based day/night, e.g. `Asia/Tbilisi` |

## Layout

```
src/standalone.py   the bridge: car emulator + glue (video/audio/touch), phone session lifecycle
src/aa_session.py   Android Auto head-unit protocol over one TCP connection (framing, TLS, channels, video mode)
src/aa_bt.py        Bluetooth hand-off for wireless AA (BlueZ profiles, RFCOMM WifiStart/WifiInfo)
src/boxd.py         Carlinkit box emulation: FunctionFS gadget + encrypted AutoKit protocol
data/session_ref.json   startup replies of a real dongle (personal data replaced, see docs/protocol.md)
config/ systemd/ scripts/   AP, DHCP, Bluetooth and service configuration, installer
tools/              fakecar.py (stand-in for the car app), Android-x86 VM runner, udev rule
docs/               protocol notes, Wi-Fi, resolution, testing, troubleshooting
```

## Known gaps

* Microphone (car → phone) works (verified: voice assistant on the phone hears the car's mic). The car-side protocol comes from a decompile of an older AutoKit version, not the 2025 one.
* Steering-wheel / head-unit media keys: forwarding to the phone is implemented (box Commands 201–205 → Android media keys), but the car tested here never sends them to the box, so it is unproven. The car's own play/pause commands that follow audio changes are deliberately not forwarded.
* Phone calls: the call voice goes over Bluetooth hands-free, not the Android Auto link, so without `AA_CALLS=1` (see [docs/calls.md](docs/calls.md), untested) it never reaches the car speakers or microphone.
* Day/night: the car only says "auto" (`night_mode` = 2), so the bridge decides by clock (`AA_NIGHT*`), not from real light or headlight state. An explicit report from the car (Command 16/17) is honoured if one ever arrives.
* Audio stop commands (navigation/voice 7, media 11, output 2) are sent when a stream goes silent so the car returns to the music volume; the timings are tuned by hand.
* The phone name / Bluetooth address replayed to the car come from a sanitized capture, not from the real phone.
* Touch mapping assumes the car app stretches the UI area over its whole view.
* Uses a published sample TLS certificate from aasdk to talk to the phone.
* Random reboots were seen on one board (suspected power supply). Not diagnosed.

## Credits and legal

Protocol knowledge comes from [aasdk](https://github.com/OpenDsh/aasdk), the
[open-android-auto](https://github.com/mrmees/open-android-auto) documentation, and analysis of one's own dongle and the
AutoKit app. This is an interoperability experiment for hardware you own; it is not affiliated with Google, Carlinkit or
the car maker. `scripts/fetch-assets.sh` downloads the Android Auto protobuf definitions and a sample TLS certificate from
aasdk at install time; they are not part of this repository.

## License

GPL-3.0-or-later. See [LICENSE](LICENSE). Copyright (C) 2026 levan1i.
