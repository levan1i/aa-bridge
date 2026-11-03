# Testing without a car

* **`tools/fakecar.py`** - stands in for the car app over USB (encrypted handshake, `Open 1920x720`, heartbeats) and saves the
  video/PCM it receives. Needs `pyusb`; run it on a PC with the Beagle connected.
* **Android-x86 in QEMU** - install the AutoKit APK in an Android-x86 9 VM and pass the Beagle's USB gadget through
  (`tools/run_android_vm.sh`, udev rule `tools/99-autobox.rules` so the device is accessible without root). AutoKit then behaves
  like the car app. The VM sends its own window size in `Open` (e.g. 1472x884), which exercises the dynamic resolution code.
* **A second Android phone** running AutoKit over USB-OTG/gadget works the same way (portrait views included).
* Phone-side logs: `adb logcat | grep -E "GH.CAR|CAR.VIDEO|WIRELESS"` explains most protocol rejections.

Gotchas: the Beagle has no RTC (log times restart each boot); `sudo` over non-interactive ssh needs `sudo -S`.
