#!/bin/bash
# normal run, with the Beagle (1314:1521) passed through to Android
cd "$(dirname "$0")"
exec qemu-system-x86_64 -enable-kvm -cpu host -smp 4 -m 4096 -machine q35 \
  -device virtio-vga-gl -display gtk,gl=on -device qemu-xhci,id=xhci -audiodev pa,id=snd0 -device intel-hda -device hda-duplex,audiodev=snd0 -device usb-tablet -monitor unix:/tmp/avm-mon.sock,server,nowait \
  -drive file=android.qcow2,if=virtio \
  -netdev user,id=n0,hostfwd=tcp::5555-:5555 -device virtio-net-pci,netdev=n0 \
  -device usb-host,bus=xhci.0,vendorid=0x1314,productid=0x1521
