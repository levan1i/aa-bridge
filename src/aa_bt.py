#!/usr/bin/env python3
"""aa_bt - make the Beagle show up to an Android phone as a *wireless Android Auto head unit*.

Follows the working recipe from mrmees/open-android-auto docs/wireless-bluetooth-setup.md:
  * BlueZ profiles: HFP AG (required: Android won't start wireless AA without a "car" HFP link), HSP HS,
    and the Android Auto RFCOMM profile with a clean SDP record (AA UUID only)
  * RFCOMM handshake: WifiStartRequest (ip, port) -> WifiInfoRequest -> WifiInfoResponse (ssid/key/bssid)
  * then the phone joins our Wi-Fi AP and connects to TCP <ip>:<port>; this milestone just accepts it and logs.
Run as root. Needs bluetoothd started with:  --compat -P '*'   (see config/bluetooth-override.conf)
"""
import asyncio, glob, os, socket, struct, sys, threading, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import phoneid
from dbus_next import Variant
from dbus_next.aio import MessageBus
from dbus_next.constants import BusType
from dbus_next.service import ServiceInterface, method

AA_UUID = "4de17a00-52cb-11e6-bdf4-0800200c9a66"
HFP_AG = "0000111f-0000-1000-8000-00805f9b34fb"
HSP_HS = "00001108-0000-1000-8000-00805f9b34fb"
HSP_AG = "00001112-0000-1000-8000-00805f9b34fb"          # the phone's side of the headset link: what we connect to
RECONNECT = os.environ.get("AA_BT_RECONNECT", "1") == "1"

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
LAST_AA = 0.0  # when the phone last opened the Android Auto Bluetooth link
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
        global LAST_AA
        log("NewConnection", self.pname, device)
        sock = socket.socket(fileno=fd)
        if self.kind == "aa":
            LAST_AA = time.time()
            asyncio.ensure_future(note_phone(device))
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


def phone_session_up():
    """The phone's Android Auto TCP connection to the bridge is established."""
    try:
        with open("/proc/net/tcp") as f:
            return any(l.split()[1].endswith(":%04X" % PORT) and l.split()[3] == "01" for l in f.readlines()[1:])
    except OSError:
        return True


def car_attached():
    """The car's USB host has configured our gadget: the car is on and wants a phone."""
    for p in glob.glob("/sys/class/udc/*/state"):
        try:
            if open(p).read().strip() == "configured":
                return True
        except OSError:
            pass
    return False


async def bluez_iface(bus, path, name):
    return bus.get_proxy_object("org.bluez", path, await bus.introspect("org.bluez", path)).get_interface(name)


async def note_phone(path):
    """Remember the phone that started Android Auto: the bridge tells the car about it."""
    try:
        props = await bluez_iface(BUS, path, "org.freedesktop.DBus.Properties")
        mac = (await props.call_get("org.bluez.Device1", "Address")).value
        name = (await props.call_get("org.bluez.Device1", "Alias")).value
        phoneid.save(mac, name)
        log("phone for the car: %s %s" % (mac, name))
    except Exception as e:
        log("phone details not saved:", repr(e))


async def reach_phone(bus):
    """Android Auto starts its wireless setup when the phone gets a Bluetooth headset link to a known car. After a restart the
    phone does not open one by itself, so open it from here. A link left over from the old session has to drop first."""
    om = await bluez_iface(bus, "/", "org.freedesktop.DBus.ObjectManager")
    devices = []
    for path, ifaces in (await om.call_get_managed_objects()).items():
        d = ifaces.get("org.bluez.Device1")
        if d and d["Paired"].value:
            devices.append((not d["Connected"].value, path, d["Connected"].value))
    for _, path, connected in sorted(devices):                # the phone that is already linked goes first
        dev = await bluez_iface(bus, path, "org.bluez.Device1")
        try:
            if connected:
                log("no Android Auto session: dropping the old Bluetooth link", path)
                await dev.call_disconnect()
                await asyncio.sleep(2)
            log("no Android Auto session: calling the phone", path)
            await dev.call_connect_profile(HSP_AG)
            return True
        except Exception as e:
            log("could not reach", path, repr(e))
    return False


async def reopen_headset(bus):
    """Android Auto runs over Wi-Fi, but calls need the Bluetooth headset link. Restarting this service drops that link and the
    phone does not reopen it by itself, so ask the last phone for it (no disconnect: the Android Auto session stays)."""
    p = phoneid.load()
    if not p:
        return
    path = "/org/bluez/hci0/dev_" + p["mac"].replace(":", "_")
    try:
        dev = await bluez_iface(bus, path, "org.bluez.Device1")
        log("Android Auto is on but calls have no headset link: asking", p["mac"], "for it")
        await dev.call_connect_profile(HSP_AG)
    except Exception as e:
        log("could not reopen the headset link:", repr(e))


async def reconnect_loop(bus):
    """While the car is on with no phone session, keep asking the paired phones to connect, less often as time goes on."""
    idle = wake = 0.0
    wait = 20
    started, hs_wake = time.time(), 0.0
    while True:
        await asyncio.sleep(3)
        now = time.time()
        busy = CALLS is not None and CALLS.active
        if (CALLS is not None and CALLS.rfcomm is None and not busy and phone_session_up()
                and now - started > 20 and now - LAST_AA > 20 and now > hs_wake):
            hs_wake = now + 60
            await reopen_headset(bus)
        if phone_session_up() or busy or not car_attached():
            idle, wake, wait = 0.0, 0.0, 20
            continue
        idle = idle or now
        if now - idle < 10 or now - LAST_AA < 30 or now < wake:
            continue
        try:
            await reach_phone(bus)
        except Exception as e:
            log("reconnect failed:", repr(e))
        wake, wait = now + wait, min(wait * 2, 120)


async def main():
    global BUS
    bus = BUS = await MessageBus(bus_type=BusType.SYSTEM, negotiate_unix_fd=True).connect()
    for path, uuid, name, kind, opts in (
        ("/aa/hfp", HFP_AG, "Hands-Free Audio Gateway", "hfp", {}),
        ("/aa/hsp", HSP_HS, "Headset", "hsp", {}),
        ("/aa/aa", AA_UUID, "Android Auto Wireless", "aa", {"Channel": Variant("q", 8), "ServiceRecord": Variant("s", AA_RECORD)}),
    ):
        bus.export(path, Profile(name, kind))
        introspect = await bus.introspect("org.bluez", "/org/bluez")
        pm = bus.get_proxy_object("org.bluez", "/org/bluez", introspect).get_interface("org.bluez.ProfileManager1")
        options = {"Name": Variant("s", name), "RequireAuthentication": Variant("b", False), "RequireAuthorization": Variant("b", False)}
        if kind == "hsp":
            options["AutoConnect"] = Variant("b", False)      # no Role: we can also open this link ourselves (reach_phone)
        else:
            options["Role"] = Variant("s", "server")
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
    if RECONNECT:
        asyncio.ensure_future(reconnect_loop(bus))
    await asyncio.get_event_loop().create_future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
