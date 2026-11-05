# Phone calls through the car's speakers and microphone (`AA_CALLS=1`)

Android Auto does not carry phone-call voice over its own link (checked in the logs: during a call the phone opens no
microphone channel and sends no call audio). Calls go over Bluetooth hands-free instead, to whatever the phone treats as the
"car kit". The Beagle is that car kit (it shows up as an HSP headset), so without this feature the call audio goes nowhere:
you may hear nothing, and the other side hears nothing.

```
caller <-> phone ==Bluetooth SCO (8 kHz mono)==> Beagle (aa_bt.py + callaudio.py)
                                                    │  local UDP 127.0.0.1:5290 / 5291
                                                    ▼
                                          standalone.py ==USB box protocol==> car speakers + car microphone
```

* `callaudio.py` (inside `aa_bt.py`): answers the headset link's AT commands with `OK`, listens for the phone's SCO connection,
  sends the caller's voice (8 kHz s16 mono) to the bridge, and writes the car's microphone back into SCO packet by packet.
* `standalone.py`: on call start sends the car an audio message `(decodeType 3 = 8 kHz mono, audioType 1)` with command 4
  (PHONECALL_START), tells the car to record the microphone (INPUT_CONFIG + Command 1), streams the caller's voice as audio
  messages, and returns the car mic. The mic is requested at 8 kHz (decodeType 3), the same as the playback: AutoKit feeds its WebRTC echo canceller the played audio only when the two decode types match, so a mismatch makes callers hear themselves. On the end it sends command 5 (PHONECALL_STOP) and
  Command 2.
* Enable with `AA_CALLS=1` in `/etc/beagle-aa-bridge/env`, then restart `aa-bt` and `standalone`.

## What the phone is told

* The Bluetooth pairing reply is written by hand: the aasdk `.proto` has its two fields swapped. The real layout is field 1 =
  status (0 = success, 1 = UNSOLICITED_MESSAGE), field 2 = already_paired. The old reply meant "unsolicited message", and the
  phone repeated its request every 120 s.
* With `AA_CALLS=1` the head unit sends `CallAvailabilityStatus{call_available=true}` (control message 24) and offers the
  phone status channel (descriptor field 10, no settings). `PhoneStatusUpdate` (0x8001) is logged as state and duration only;
  numbers and names are not logged.

## Tested so far

Normal phone calls and Facebook Messenger calls work through the car's speakers and microphone (SCO link, about 340 packets/s
each way). Google Meet did not. The Android Auto call screen did not appear before the changes above; they are not yet confirmed.

## Diagnosing a quiet call

The log line at the end of each call gives the loudest sample each way, and how many packets had no car mic audio ready.
`AA_CALL_MIC_GAIN` (default 6) amplifies the car mic. `sudo touch /run/aa-call-tone` makes the next call send a 440 Hz tone
instead of the car mic, which shows whether the path to the phone works at all; `sudo rm /run/aa-call-tone` turns it off.

## Limits / what to check

* SCO over a USB Bluetooth adapter needs the kernel's SCO-over-HCI path; if the log shows `SCO link from ...` but no audio, the
  adapter or its alternate setting may be at fault. Voice is narrow-band (CVSD, 8 kHz): it sounds like a phone call.
* The log should show: `call audio: SCO link from ...`, `call: audio link up -> car told call start + record`, then
  `call audio: SCO closed (N packets in, N out)`. No `SCO link` line means the phone never routed the call to the Beagle.
* The phone only routes calls (and apps such as Google Meet) to the Beagle if Android treats it as the active hands-free
  device; check the phone's Bluetooth settings for the Beagle ("Phone calls" must be on).
* The assistant's microphone use and a call at the same time are not handled.
* Wide-band (mSBC) calls are not supported.
