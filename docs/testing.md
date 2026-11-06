# Testing without a car

* **`tools/fakecar.py`** - stands in for the car over USB: repeats the real car's startup, logs every frame, and saves the video, PCM
  and covers. Needs `pyusb`. Run it on a PC with the Beagle connected, or on the Beagle with a real box plugged in, with `standalone`
  and `aa-bt` stopped so the phone goes to the box. A box with no car resets about every 12 s until the startup arrives.
* **Android-x86 in QEMU** - install the AutoKit APK in an Android-x86 9 VM and pass the Beagle's USB gadget through
  (`tools/run_android_vm.sh`, udev rule `tools/99-autobox.rules` so the device is accessible without root). AutoKit then behaves
  like the car app. The VM sends its own window size in `Open` (e.g. 1472x884), which exercises the dynamic resolution code.
* **A second Android phone** running AutoKit over USB-OTG/gadget works the same way (portrait views included).
* Phone-side logs: `adb logcat | grep -E "GH.CAR|CAR.VIDEO|WIRELESS"` explains most protocol rejections.

Gotchas: the Beagle has no RTC (log times restart each boot); `sudo` over non-interactive ssh needs `sudo -S`.
