#!/usr/bin/env python3
"""aa_bt - make the Beagle show up to an Android phone as a *wireless Android Auto head unit*.

Follows the working recipe from mrmees/open-android-auto docs/wireless-bluetooth-setup.md:
  * BlueZ profiles: HFP AG (required: Android won't start wireless AA without a "car" HFP link), HSP HS,
    and the Android Auto RFCOMM profile with a clean SDP record (AA UUID only)
  * RFCOMM handshake: WifiStartRequest (ip, port) -> WifiInfoRequest -> WifiInfoResponse (ssid/key/bssid)
  * then the phone joins our Wi-Fi AP and connects to TCP <ip>:<port>; this milestone just accepts it and logs.
Run as root. Needs bluetoothd started with:  --compat -P '*'   (see config/bluetooth-override.conf)
"""
import asyncio, os, socket, struct, sys, threading, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dbus_next import Variant
from dbus_next.aio import MessageBus
from dbus_next.constants import BusType
from dbus_next.service import ServiceInterface, method

AA_UUID = "4de17a00-52cb-11e6-bdf4-0800200c9a66"
HFP_AG = "0000111f-0000-1000-8000-00805f9b34fb"
HSP_HS = "00001108-0000-1000-8000-00805f9b34fb"

IP = os.environ.get("AA_IP", "10.0.0.1")
PORT = int(os.environ.get("AA_PORT", "5288"))
SSID = os.environ.get("AA_SSID", "WirelessAADongle")
KEY = os.environ.get("AA_KEY", "ConnectAAWirelessDongle")
def _ap_bssid():
    """BSSID of the access point the phone must join: AA_BSSID, else the MAC of the AP interface (AA_AP_IFACE)."""
    if os.environ.get("AA_BSSID"):
        return os.environ["AA_BSSID"]
    try:
        return open("/sys/class/net/%s/address" % os.environ.get("AA_AP_IFACE", "wlan2")).read().strip().upper()
    except OSError:
        return "12:CA:BF:DA:53:08"


BSSID = _ap_bssid()
ALIAS = os.environ.get("AA_ALIAS", "Beagle Auto")

T0 = time.time()


def log(*a):
    print("%7.2f" % (time.time() - T0), *a, flush=True)


AA_RECORD = """<?xml version="1.0" encoding="UTF-8" ?>
<record>
  <attribute id="0x0001"><sequence><uuid value="%s" /></sequence></attribute>
  <attribute id="0x0004">
    <sequence>
      <sequence><uuid value="0x0100" /></sequence>
      <sequence><uuid value="0x0003" /><uint8 value="8" /></sequence>
    </sequence>
  </attribute>
  <attribute id="0x0005"><sequence><uuid value="0x1002" /></sequence></attribute>
  <attribute id="0x0100"><text value="Android Auto Wireless" /></attribute>
</record>""" % AA_UUID


# ---- protobuf by hand (tiny messages)
def varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F; n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def pb_str(field, s):
    b = s.encode()
    return bytes([(field << 3) | 2]) + varint(len(b)) + b


def pb_int(field, n):
    return bytes([(field << 3) | 0]) + varint(n)


def pb_wifi_start(ip, port):
    return pb_str(1, ip) + pb_int(2, port)


def pb_wifi_info(ssid, key, bssid):
    return pb_str(1, ssid) + pb_str(2, key) + pb_str(3, bssid) + pb_int(4, 8) + pb_int(5, 1)   # WPA2_PERSONAL, DYNAMIC


def read_exact(sock, n):
    buf = b""
    while len(buf) < n:
        c = sock.recv(n - len(buf))
        if not c:
            raise ConnectionError("closed")
        buf += c
    return buf


def handshake(sock, who):
    """RFCOMM: [len:u16 be][msg_id:u16 be][protobuf]"""
    sock.setblocking(True)
    sock.settimeout(15.0)

    def send(mid, payload):
        sock.sendall(struct.pack(">HH", len(payload), mid) + payload)

    try:
        send(1, pb_wifi_start(IP, PORT))
        log("[%s] -> WifiStartRequest %s:%d" % (who, IP, PORT))
        while True:
            ln, mid = struct.unpack(">HH", read_exact(sock, 4))
            pl = read_exact(sock, ln) if ln else b""
            log("[%s] <- msg_id=%d len=%d %s" % (who, mid, ln, pl.hex()[:60]))
            if mid == 2:                                              # WifiInfoRequest
                send(3, pb_wifi_info(SSID, KEY, BSSID))
                log("[%s] -> WifiInfoResponse ssid=%s bssid=%s" % (who, SSID, BSSID))
            elif mid == 6:                                            # WifiConnectStatus
                log("[%s] <- WifiConnectStatus: phone joined the Wi-Fi (status payload %s)" % (who, pl.hex()))
                return True
            elif mid == 7:
                log("[%s] <- WifiStartResponse" % who)
    except (OSError, ConnectionError) as e:
        log("[%s] handshake ended: %r" % (who, e))
        return False


HELD = []     # sockets we must keep open (HFP/HSP)
CALLS = None  # call audio relay (SCO <-> bridge), only when AA_CALLS=1


def _calls():
    global CALLS
    import callaudio
    if CALLS is None and callaudio.ENABLED:
        CALLS = callaudio.CallAudio(log)
    return CALLS


class Profile(ServiceInterface):
    def __init__(self, name, kind):
        super().__init__("org.bluez.Profile1")
        self.pname, self.kind = name, kind

    @method()
    def Release(self):
        log("profile released", self.pname)

    @method()
    def NewConnection(self, device: "o", fd: "h", fd_properties: "a{sv}"):
        log("NewConnection", self.pname, device)
        sock = socket.socket(fileno=fd)
        if self.kind == "aa":
            threading.Thread(target=handshake, args=(sock, self.pname), daemon=True).start()
        else:
            sock.setblocking(True)
            HELD.append(sock)                 # HFP/HSP: just keep the link open
            if self.kind == "hsp" and _calls():
                _calls().attach_rfcomm(sock)  # ... and answer its AT commands so the phone keeps the headset

    @method()
    def RequestDisconnection(self, device: "o"):
        log("RequestDisconnection", self.pname, device)


VIDEO_DUMP = os.environ.get("AA_VIDEO_DUMP", "/tmp/aa_video.h264")


async def tcp_server():
    import aa_session
    dump = {"f": None, "n": 0}

    def on_video(ts, data):
        if dump["f"] is None:
            dump["f"] = open(VIDEO_DUMP, "wb")
        if dump["n"] < 12 << 20:                       # keep the first 12 MB for inspection
            dump["f"].write(data); dump["n"] += len(data)
            if dump["n"] >= 12 << 20:
                dump["f"].close()

    async def on_conn(reader, writer):
        dump["f"], dump["n"] = None, 0
        try:
            await aa_session.handle_client(reader, writer, on_video=on_video)
        except Exception as e:
            log("session error:", repr(e))
    srv = await asyncio.start_server(on_conn, "0.0.0.0", PORT)
    log("TCP server listening on 0.0.0.0:%d (Android Auto head-unit session)" % PORT)
    return srv


async def main():
    bus = await MessageBus(bus_type=BusType.SYSTEM, negotiate_unix_fd=True).connect()
    for path, uuid, name, kind, opts in (
        ("/aa/hfp", HFP_AG, "Hands-Free Audio Gateway", "hfp", {}),
        ("/aa/hsp", HSP_HS, "Headset", "hsp", {}),
        ("/aa/aa", AA_UUID, "Android Auto Wireless", "aa", {"Channel": Variant("q", 8), "ServiceRecord": Variant("s", AA_RECORD)}),
    ):
        bus.export(path, Profile(name, kind))
        introspect = await bus.introspect("org.bluez", "/org/bluez")
        pm = bus.get_proxy_object("org.bluez", "/org/bluez", introspect).get_interface("org.bluez.ProfileManager1")
        options = {"Name": Variant("s", name), "Role": Variant("s", "server"),
                   "RequireAuthentication": Variant("b", False), "RequireAuthorization": Variant("b", False)}
        options.update(opts)
        await pm.call_register_profile(path, uuid, options)
        log("registered profile", name, uuid)

    ad = await bus.introspect("org.bluez", "/org/bluez/hci0")
    props = bus.get_proxy_object("org.bluez", "/org/bluez/hci0", ad).get_interface("org.freedesktop.DBus.Properties")
    for k, v in (("Powered", Variant("b", True)), ("Alias", Variant("s", ALIAS)), ("Pairable", Variant("b", True)),
                 ("PairableTimeout", Variant("u", 0)), ("DiscoverableTimeout", Variant("u", 0)), ("Discoverable", Variant("b", True))):
        try:
            await props.call_set("org.bluez.Adapter1", k, v)
        except Exception as e:
            log("adapter property %s not set: %s" % (k, e))
    log("adapter hci0 powered, discoverable as '%s'" % ALIAS)
    if _calls():
        try:
            addr = (await props.call_get("org.bluez.Adapter1", "Address")).value
            _calls().start(addr)
        except Exception as e:
            log("call audio not started:", repr(e))
    if not os.environ.get("AA_NO_TCP"):
        await tcp_server()
    log("ready: pair the phone with '%s' and wait for the Android Auto prompt" % ALIAS)
    await asyncio.get_event_loop().create_future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
