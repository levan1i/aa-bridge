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
  (PHONECALL_START) then command 1 (OUTPUT_START, which is what sets up AutoKit's call player), tells the car to record the microphone (INPUT_CONFIG + Command 1), streams the caller's voice as audio
  messages, and returns the car mic. Formats on the car link are set from what the test car did:

  | Playback | Mic | Result on the car |
  |---|---|---|
  | 8 kHz | 16 kHz | both sides clear; the caller hears an echo (AutoKit's canceller is off) |
  | 8 kHz | 8 kHz | the other side hears a robotic voice |
  | 16 kHz | 16 kHz | the other side is clear; playback is stretched, late and outlives the call |

  The default is the first row (`AA_CALL_PLAY_DEC=3`, `AA_CALL_MIC_DEC=5`). AutoKit feeds its echo canceller only when the two
  decode types match, so `callaudio.py` suppresses the echo itself: it turns the mic down while the caller is talking
  (`AA_CALL_DUCK`, `AA_CALL_DUCK_LEVEL`, `AA_CALL_DUCK_HOLD`). This is half-duplex, like a speakerphone.
* Enable with `AA_CALLS=1` in `/etc/beagle-aa-bridge/env`, then restart `aa-bt` and `standalone`.

## What the phone is told

* The Bluetooth pairing reply is written by hand: the aasdk `.proto` has its two fields swapped. The real layout is field 1 =
  status (0 = success, 1 = UNSOLICITED_MESSAGE), field 2 = already_paired. The old reply meant "unsolicited message", and the
  phone repeated its request every 120 s.
* With `AA_CALLS=1` the head unit sends `CallAvailabilityStatus{call_available=true}` (control message 24) and offers the
  phone status channel (descriptor field 10, no settings). `PhoneStatusUpdate` (0x8001) is logged as state and duration only;
  numbers and names are not logged.

## Call signalling to the car

The phone's status drives the car, because it is the only signal before the audio link exists:

* `INCOMING` -> audio command 14; in a call -> 4; no calls and no audio link -> 5. App calls (Meet, Messenger) report no status, so
  for them the audio link alone drives 4 and 5.
* Caller details: message type 0x2A (never encrypted), `<I 100>` + JSON + NUL, following the firmware's `phoneStatusUpdate`
  (`ARMAndroidAuto`), sent on every phone update during a call. `CallStatus` is 2 incoming, 4 in call or conference, 5 on hold,
  3 muted, 0 otherwise. `CallDirection` is 1 if the call rang, else 2. `CallNumber` and `CallID` (the same value) and `CallName` appear
  only if the phone gave them. When the calls end, one message has status 0 with the last number and name; the first update of a session
  does the same with empty strings. The car may ignore all of it; `AA_CALL_INFO=0` turns it off. Numbers and names are not logged.
* Car commands 300 (answer) and 301 (hang up) become Android keys 5 and 6. If the phone's state has not changed a second later,
  `AT+CKPD=200` presses the headset button. That button toggles, so it is never used to reject a ringing call.

Audio commands 12 and 13 (decode type 4, audio type 1) go around the ringing, as CarPlay captures show. `ARMAndroidAuto` has no path for them, so a real dongle may not send them in Android Auto mode. `AA_CALL_ALERT=0` turns them off.

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
