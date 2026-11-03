#!/bin/bash
# Make sure the USB Wi-Fi adapter that runs the car access point has exactly one interface, named $AA_AP_IFACE.
# (iwd/udev sometimes fail to create it at boot - "NEW_INTERFACE failed: Too many open files in system" -
#  and a stray second interface on the same radio stops hostapd.)
IFACE="${AA_AP_IFACE:-wlan2}"
DRV="${AA_AP_DRIVER:-rtw88}"        # kernel driver of the AP adapter (RTL8811AU/8821AU = rtw88_8821au)
for i in $(seq 1 60); do
  for p in /sys/class/ieee80211/phy*; do
    [ -e "$p/device/driver" ] || continue
    case "$(readlink -f "$p/device/driver")" in *"$DRV"*) ;; *) continue ;; esac
    ph=$(basename "$p")
    for n in "$p"/device/net/* "$p"/net/*; do
      [ -e "$n" ] || continue
      n=$(basename "$n"); [ "$n" != "$IFACE" ] && /usr/sbin/iw dev "$n" del
    done
    [ -d "/sys/class/net/$IFACE" ] && exit 0
    /usr/sbin/iw phy "$ph" interface add "$IFACE" type managed && sleep 1
  done
  sleep 1
done
[ -d "/sys/class/net/$IFACE" ]
