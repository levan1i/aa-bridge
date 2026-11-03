# Using another board

Tested: **BeagleY-AI only** (with a TP-Link UB500 Bluetooth adapter and a TP-Link Archer T2U Nano Wi-Fi adapter, because its
on-board Wi-Fi is 2.4 GHz and its on-board Bluetooth is BLE-only). Nothing below has been verified on other hardware.

## Requirements

1. **USB device mode** on a connector you can plug into the car, with Linux gadget support (`libcomposite`, FunctionFS).
2. **Wi-Fi: 5 GHz + AP mode.** Check with `iw phy <phy> info`: "Supported interface modes" must list `AP`, and 5 GHz
   channels must be present and not "no IR" in your regulatory domain. Without 5 GHz the phone refuses 1080p (docs/wifi.md).
3. **Bluetooth classic (BR/EDR)** with HFP; BLE-only controllers do not work for wireless Android Auto.
4. **Power**: the car's USB port gives ~5 V at a limited current. Boards that peak above it brown out and reboot.
5. CPU is not critical: video is forwarded without transcoding.

## Boards that may fit (unverified)

| Board | Wi-Fi / Bluetooth | Notes |
|---|---|---|
| Radxa Zero 3W | AP6256: dual-band, BT 5.0 | USB-C OTG |
| Raspberry Pi CM4 (wireless) + IO board, Pi 4B | CYW43455: dual-band, BT 5.0 | well documented; gadget mode on OTG / USB-C |
| Orange Pi Zero 2W | AP6256: dual-band, BT 5.0 | USB-C OTG, vendor kernel |
| Raspberry Pi Zero 2 W | 2.4 GHz only, BT 4.2 | lowest power; 720p only |

## What to change

| Setting | Where | BeagleY-AI value |
|---|---|---|
| USB device controller | `standalone.py --udc <name>` (list: `ls /sys/class/udc`); put it in `systemd/standalone.service` | `31000000.usb` |
| AP interface | `AA_AP_IFACE` in `/etc/beagle-aa-bridge/env`, `--iface` for `install.sh` | `wlan2` (USB adapter) |
| AP adapter driver | `AA_AP_DRIVER` in `env` | `rtw88` |
| Bluetooth adapter | the code uses `hci0`; make sure the right controller is `hci0` | UB500 (RTL8761B) |

On boards with built-in Wi-Fi the `aa-mkap.sh` / `aa-iwd.sh` helpers (they work around `iwd` and a USB adapter at boot) are
usually unnecessary: skip `config/iwd-override.conf` and, if the interface already exists, `aa-mkap.sh` does nothing.
Please report what you had to change, and which parts of the install worked, so this list can become verified.
