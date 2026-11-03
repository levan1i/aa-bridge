#!/bin/bash
# Start iwd managing only the Wi-Fi radios that are NOT the car AP adapter ($AA_AP_DRIVER).
DRV="${AA_AP_DRIVER:-rtw88}"
for i in $(seq 1 60); do
  P=""
  for p in /sys/class/ieee80211/phy*; do
    [ -e "$p/device/driver" ] || continue
    case "$(readlink -f "$p/device/driver")" in *"$DRV"*) ;; *) P="$P,$(basename "$p")" ;; esac
  done
  [ -n "$P" ] && exec /usr/libexec/iwd -p "${P#,}"
  sleep 1
done
exec /usr/libexec/iwd
