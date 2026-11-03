# Protocol notes

## Carlinkit / AutoKit USB protocol (car side, `boxd.py`)

* USB identity `1314:1521`, strings "Magic Communication Tec." / "Auto Box", one vendor interface, bulk IN `0x81` / OUT `0x01`.
* Message: 16-byte header `magic(0x55AA55AA plain | 0x55BB55BB encrypted) · length · type · ~type`, then the payload.
* Types: Open 1, Plugged 2, Phase 3, Unplugged 4, Touch 5, Video 6, Audio 7, Command 8, BoxSettings 0x19,
  multi-touch 0x17, SendFile 0x99, Heartbeat 0xAA, AppInfo 0xA0, SoftwareVersion 0xCC, Encrypt 0xF0.
* After the `0xF0` nonce handshake everything is AES-128-CFB128. Key: `key[i] = "SkBRDy3gmrw1ieH0"[(nonce+i) % 16]`.
  The real dongle expects a heartbeat about every 10 s.
* Video message: `<5I w, h, flags(3), timestamp, 0` + one H.264 access unit (Annex B, SPS/PPS before each IDR).
* Audio message: 12-byte header `<IfI decodeType, volume, audioType` + PCM, or a single command byte (13 bytes total).
* Touch (type 5): `<III action, x, y` with x/y in 1/10000 of the car view; action 14/15/16 = down/move/up.
  Multi-touch (0x17): `<ffII x, y, action, id`.
* The car app sends `android_work_mode` (SendFile), `Open` (width, height, fps, format…), `BoxSettings` (JSON, including
  `androidAutoSizeW/H`) and `AppInfo`. It retries `Open` about a second later with a malformed payload, which crashed
  the real dongle; the emulator answers it like the last good one.
* The IM L6 app shows only the **top 720 rows** of the frame. It crashed on 1280x720 streams and on streams whose SPS
  has cropping, and accepts the uncropped 1920x1080 frame the phone produces.
* `data/session_ref.json` is a decoded capture of the real dongle's startup replies. The phone's Bluetooth address and
  name, the dongle UUID and the model strings were replaced with same-length placeholders, because the file is replayed
  to the car (`announce_phone`).

## Android Auto head-unit protocol (phone side, `aa_session.py`)

* Frame `[channel:1][flags:1][size:2 BE]([total:4 BE] if FIRST)[payload]`; flags BULK=3, FIRST=1, MIDDLE=0, LAST=2,
  `|0x04` control, `|0x08` encrypted. Payload `[msg_id:2 BE][protobuf]`.
* Start: VERSION (head unit 1.1, phone answers 1.7) → TLS 1.2 handshake carried in SSL_HANDSHAKE messages (the head unit
  is the TLS *client*) → AUTH_COMPLETE → the phone sends SERVICE_DISCOVERY_REQUEST → channel opens → AV setup.
* Channels: 0 control, 1 input, 2 sensor, 3 video, 4 media audio, 5 speech audio, 6 system audio, 7 microphone, 8 Bluetooth.
* **Decrypt every TLS frame individually, in wire order.** Frames of different channels interleave; decrypting after
  reassembling a fragmented message broke the TLS record layer (`RECORD_LAYER_FAILURE`, session died after ~3 s).
* In wireless mode the service discovery must also contain a BluetoothChannel (the adapter address, pairing methods
  A2DP+HFP). The phone then sends a BluetoothPairingRequest, answered with `already_paired=True`.

## Wireless start (`aa_bt.py`)

BlueZ with `--compat -P '*'`; the core SDP records `0x10000–0x10003` are deleted (Android rejects their 128-bit encoding of
16-bit UUIDs); profiles: HFP audio gateway `111f` (Android refuses wireless AA without a "car" HFP link), HSP `1108`, and the
Android Auto RFCOMM service `4de17a00-52cb-11e6-bdf4-0800200c9a66` (channel 8).
RFCOMM exchange: `WifiStartRequest(ip, port 5288)` → `WifiInfoRequest` → `WifiInfoResponse(ssid, key, bssid, WPA2, DYNAMIC)`
→ `WifiConnectStatus`; the phone then joins the AP and connects to `ip:5288`.
