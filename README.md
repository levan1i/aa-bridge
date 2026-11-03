# beagle-aa-bridge

A **BeagleY-AI that replaces a Carlinkit-style wireless Android Auto dongle**.

The car's USB-A/USB-C port sees a `1314:1521` "Auto Box" (the Carlinkit CPC200-CCPA / A15W identity the **AutoKit**
app expects). The Beagle itself is the **Android Auto head unit** for the phone, over wireless Android Auto
(Bluetooth hand-off, then Wi-Fi). No dongle hardware is involved.

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

* The audio *start commands* (media 10/1, navigation 6/1) are guesses; only their length was seen from the real dongle.
* Microphone (car → phone) is not implemented.
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
