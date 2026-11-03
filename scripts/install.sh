#!/bin/bash
# Install the bridge on a BeagleY-AI (Debian). Run as root from a checkout of this repo:
#   sudo scripts/install.sh [--iface wlan2] [--ssid WirelessAADongle] [--channel 36] [--country GE]
# Re-running is safe: an existing /etc/beagle-aa-bridge/env (and its Wi-Fi password) is kept.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run as root (sudo)"; exit 1; }
SRC=$(cd "$(dirname "$0")/.." && pwd)
DEST=/opt/beagle-aa-bridge
CONF=/etc/beagle-aa-bridge
IFACE=wlan2; SSID=WirelessAADongle; CHANNEL=36; COUNTRY=GE
while [ $# -gt 0 ]; do
  case "$1" in
    --iface) IFACE=$2; shift 2;; --ssid) SSID=$2; shift 2;;
    --channel) CHANNEL=$2; shift 2;; --country) COUNTRY=$2; shift 2;;
    *) echo "unknown option $1"; exit 1;;
  esac
done
# centre channel of the 80 MHz block that contains the primary channel (UNII-1: 36-48 -> 42, UNII-3: 149-161 -> 155)
case "$CHANNEL" in 36|40|44|48) VHT=42;; 52|56|60|64) VHT=58;; 100|104|108|112) VHT=106;; 149|153|157|161) VHT=155;; *) echo "use a 5 GHz channel (36-48, 52-64, 100-112, 149-161)"; exit 1;; esac

echo "== packages"
apt-get update -qq
apt-get install -y python3-protobuf protobuf-compiler python3-cryptography python3-dbus-next \
    hostapd dnsmasq bluez bluez-tools iw git openssl
systemctl disable --now hostapd dnsmasq 2>/dev/null || true   # the stock services would fight ours

echo "== files -> $DEST"
mkdir -p "$DEST"
cp -a "$SRC/src" "$SRC/data" "$SRC/scripts" "$DEST/"
[ -d "$DEST/gen" ] && [ -f "$DEST/certs/hu_key.pem" ] || {
  [ -d "$SRC/gen" ] && [ -f "$SRC/certs/hu_key.pem" ] || "$SRC/scripts/fetch-assets.sh"
  cp -a "$SRC/gen" "$SRC/certs" "$DEST/"
}
install -m 755 "$SRC/scripts/aa-mkap.sh" "$SRC/scripts/aa-iwd.sh" /usr/local/sbin/

echo "== config -> $CONF"
mkdir -p "$CONF"
if [ ! -f "$CONF/env" ]; then
  PSK=$(openssl rand -hex 12)
  cat > "$CONF/env" <<ENV
# Read by the services. The phone learns SSID/password over Bluetooth, you never type them on the phone.
AA_SSID=$SSID
AA_KEY=$PSK
AA_AP_IFACE=$IFACE
AA_AP_DRIVER=rtw88
# AA_DPI=160
# AA_RES=3      # force an Android Auto video size instead of following the car (1/2/3 landscape, 5/6/7 portrait)
ENV
  chmod 600 "$CONF/env"
fi
set -a; . "$CONF/env"; set +a
sed -e "s/@IFACE@/$AA_AP_IFACE/" -e "s/@COUNTRY@/$COUNTRY/" -e "s/@CHANNEL@/$CHANNEL/" \
    -e "s/@VHT_CENTER@/$VHT/" -e "s/@SSID@/$AA_SSID/" -e "s/@PSK@/$AA_KEY/" \
    "$SRC/config/hostapd-aa.conf.in" > "$CONF/hostapd.conf"
chmod 600 "$CONF/hostapd.conf"
sed -e "s/^interface=.*/interface=$AA_AP_IFACE/" "$SRC/config/dnsmasq-aa.conf" > "$CONF/dnsmasq.conf"
mkdir -p /etc/systemd/network
sed -e "s/^Name=.*/Name=$AA_AP_IFACE/" "$SRC/config/00-aa-unmanaged.network" > /etc/systemd/network/00-aa-unmanaged.network
mkdir -p /etc/systemd/system/bluetooth.service.d
cp "$SRC/config/bluetooth-override.conf" /etc/systemd/system/bluetooth.service.d/aa-override.conf

echo "== services"
cp "$SRC"/systemd/*.service /etc/systemd/system/
systemctl daemon-reload
systemctl restart bluetooth
systemctl enable aa-hostapd aa-dnsmasq aa-bt-agent aa-bt standalone
systemctl restart aa-hostapd aa-dnsmasq aa-bt-agent aa-bt standalone     # restart: picks up new config/password on re-runs
sleep 3
systemctl --no-pager --lines=0 status aa-hostapd aa-dnsmasq aa-bt-agent aa-bt standalone | grep -E "^●|Active:" || true
echo
echo "Done. Wi-Fi SSID: $AA_SSID   password: $AA_KEY   (kept in $CONF/env)"
echo "Pair the phone with Bluetooth device 'Beagle Auto', plug the Beagle into the car's USB port."
echo "Logs: /var/log/standalone.log  /var/log/aa_bt.log"
