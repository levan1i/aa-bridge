# Troubleshooting

| Symptom | Check |
|---|---|
| Phone does not see "Beagle Auto" | `hciconfig hci0` is `UP RUNNING PSCAN ISCAN`; BlueZ runs with `--compat -P *`; `/var/log/aa_bt.log` shows "ready" |
| Phone pairs but never starts Android Auto | `sdptool browse local` shows only the AA, HFP-AG and HSP records (core records `0x1000x` deleted) |
| Phone drops the session ~1 s after joining Wi-Fi | link is 2.4 GHz: use 5 GHz (see docs/wifi.md) |
| Session dies after ~3 s with `SSL RECORD_LAYER_FAILURE` | decrypting reassembled messages instead of single frames (fixed in `read_message`) |
| `aa-hostapd` fails at boot | `ip -br link`: AP interface missing or duplicated; `aa-mkap.sh`; see docs/wifi.md |
| Car app shows nothing / restarts | `/var/log/standalone.log`: `Open` received? "announced the phone to the car"? "video synced on a keyframe"? |
| Random reboots | power supply; add `systemd/optional/aa-logsync.service` so logs survive power cuts |
| Green/garbled picture | the car app needs the uncropped 1920x1080 stream with SPS/PPS before each IDR |
