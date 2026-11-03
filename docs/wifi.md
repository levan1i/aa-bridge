# Wi-Fi

## Why 5 GHz

Android Auto checks the wireless frequency before accepting a video configuration. Over 2.4 GHz the phone logs
`GH.CAR.VIDEO: VideoCodecResolutionType 3 (3) is not allowed due wireless frequency` / `CAR.VIDEO: No working
configuration` / `Protocol error 2`, and closes the session after ~1 s (it then retries every second). 720p is allowed
on 2.4 GHz; 1080p is not. The on-board CC33xx Wi-Fi of the BeagleY-AI is 2.4 GHz only (channels 1-13), so a USB adapter
with 5 GHz **and AP mode** is needed (`iw phy <phy> info`: "Supported interface modes: AP", 5 GHz channels listed).
(If you are limited to 2.4 GHz, set `AA_RES=2` so the phone sends 720p. The IM L6 app crashed on a plain 1280x720 stream,
so a 2.4 GHz setup also needs the 720p stream padded into a 1920x1080 frame, e.g. with ffmpeg
`-vf pad=1920:1080:320:0` re-encoded as baseline H.264 with `aud=1:repeat-headers=1`. An earlier iteration did this and ran
at ~27 fps in software on the Beagle; it is not part of this repo.)

## The access point

`aa-hostapd` runs `/etc/beagle-aa-bridge/hostapd.conf` (rendered from `config/hostapd-aa.conf.in`) on `$AA_AP_IFACE`
at `10.0.0.1/24`; `aa-dnsmasq` hands the phone one lease. The BSSID the phone is told over Bluetooth is the MAC of that
interface (override with `AA_BSSID`), so the two always match.

## Boot-time pitfalls (seen with the rtw88 USB adapter + iwd)

* `iwd` may fail to create the adapter's interface at boot ("NEW_INTERFACE failed: Too many open files in system"), and
  a second interface on the same radio stops hostapd ("Name not unique"). `scripts/aa-mkap.sh` (run before hostapd)
  creates exactly one interface named `$AA_AP_IFACE`.
* If iwd manages your internet Wi-Fi, keep it away from the AP adapter: install `config/iwd-override.conf` as
  `/etc/systemd/system/iwd.service.d/aa.conf` (it starts iwd only on the *other* radios via `scripts/aa-iwd.sh`).
  Also `systemctl enable iwd`: if it is merely D-Bus activated it starts minutes late.
* Keep `systemd-networkd` off the AP interface (`config/00-aa-unmanaged.network`, installed by the script).
* The on-board Wi-Fi can be the client for internet: `config/wlan-client.network.example`.
