"""
Minimal pure-Python INDI client — talks to any INDI server directly over its
documented XML-over-TCP wire protocol (https://docs.indilib.org/protocol/),
without any C++ bindings. Used by mount.py's IndiMountController, primarily
to drive a ZWO AM3/AM5 via a local `indiserver` running `indi_lx200am5` —
the Linux-friendly alternative to AM5Controller's Windows-only ASCOM path.

Why not `pyindi-client`? Its pre-generated SWIG wrapper is incompatible with
current libindi releases — INDI::WidgetView became a template class between
libindi v1.9.3 and v1.9.9, and the bundled wrapper was never regenerated for
it. This is an open, unresolved upstream bug as of mid-2026 with no
workaround (indilib/pyindi-client#63, #44 — both open, no fix). Speaking the
wire protocol directly sidesteps that permanently and needs nothing beyond
the Python standard library — no libindi-dev/swig/compiler at all, which
also fits this project's pure-pip dependency style better.

Devices are discovered by role (mount/camera/GPS), not by name, using the
standard DRIVER_INFO.DRIVER_INTERFACE text property every INDI driver
publishes — a bitmask of the DRIVER_INTERFACE enum (values confirmed against
a real libindi install's basedevice.h). Device *names* vary by
driver/firmware ("EQMac", "LX200 AM5", etc.), so this is the only reliable
way to identify roles without hardcoding them.

NOTE: written against the documented protocol and standard property
semantics. Every message received is logged at DEBUG, and every device
discovered is logged at INFO on connect — if a goto misbehaves, enable
DEBUG logging for "gs.indi" and compare against what the driver actually
sends, or inspect its tree ahead of time from any box on the same network
with:

    indi_getprop -h <indiserver-host> -p 7624
"""
from __future__ import annotations

import logging
import socket
import threading
import time
import xml.parsers.expat
from xml.sax.saxutils import escape as _esc, quoteattr as _q

logger = logging.getLogger("gs.indi")

DEFAULT_INDI_PORT = 7624
_PROPERTY_TIMEOUT_S = 10.0
_RECV_CHUNK = 65536

# Driver interface bitmask — from libindi's basedevice.h (INDI::BaseDevice::DRIVER_INTERFACE)
TELESCOPE_INTERFACE = 1 << 0

_VECTOR_KINDS = ("Text", "Number", "Switch", "Light")


def _parse_number(text: str) -> float:
    """
    Parse an INDI numberValue: plain int/float, or INDI sexagesimal
    (space/colon/semicolon separated components, e.g. "12:30:15.5").
    """
    text = text.strip()
    sep = next((c for c in (":", ";", " ") if c in text), None)
    if sep is None:
        return float(text)
    parts = [p for p in text.split(sep) if p != ""]
    sign = -1.0 if parts[0].strip().startswith("-") else 1.0
    value = 0.0
    for i, p in enumerate(parts):
        value += abs(float(p)) / (60.0 ** i)
    return sign * value


class _XmlStream:
    """
    Feeds raw bytes into an expat parser and calls `on_element(node)` once
    per *complete top-level* INDI message, where
    node = {"tag": str, "attrs": dict, "text": str, "children": [node, ...]}.

    INDI's wire format is a bare sequence of top-level XML elements with no
    enclosing root, which a plain expat parser requires. The fix used here
    (and the *only* one that survived real-hardware testing — see below) is
    to inject one synthetic wrapper root immediately on construction and
    never close it, so every real message is a depth-1 child of it. Expat
    then only ever sees a single, perpetually-open document for the whole
    connection lifetime, and top-level completion is just "stack depth back
    to zero" — no multi-document handling needed at all.

    An earlier version of this class instead created a *fresh* expat parser
    each time a top-level element closed (to sidestep expat's "junk after
    document element" error for the next sibling). That looked correct and
    passed synthetic tests, but broke against a real ASIAIR: `buffer_text`
    lets expat internally look ahead a little past a closing tag before it
    flushes the queued Start/EndElementHandler callbacks, so under byte-ish
    TCP fragmentation the old parser instance would already be consuming
    bytes belonging to the *next* message before its own EndElementHandler
    fired — and swapping to a brand-new parser at that point silently
    dropped those already-consumed bytes, corrupting everything after it.
    Not swapping parsers at all removes the failure mode entirely.
    """

    _ROOT_TAG = "indi-stream"   # never sent by any real server — just our wrapper

    def __init__(self, on_element) -> None:
        self._on_element = on_element
        parser = xml.parsers.expat.ParserCreate()
        parser.buffer_text = True
        parser.StartElementHandler = self._start
        parser.EndElementHandler = self._end
        parser.CharacterDataHandler = self._chardata
        self._parser = parser
        self._stack: list[dict] = []
        self._root_seen = False
        parser.Parse(f"<{self._ROOT_TAG}>".encode(), False)

    def _start(self, tag, attrs) -> None:
        if not self._root_seen:
            self._root_seen = True   # our own synthetic root — not a real message
            return
        self._stack.append({"tag": tag, "attrs": dict(attrs), "text": [], "children": []})

    def _chardata(self, data) -> None:
        if self._stack:
            self._stack[-1]["text"].append(data)

    def _end(self, tag) -> None:
        node = self._stack.pop()
        node["text"] = "".join(node["text"])
        if self._stack:
            self._stack[-1]["children"].append(node)
        else:
            self._on_element(node)

    def feed(self, data: bytes) -> None:
        self._parser.Parse(data, False)


class IndiConnection:
    """
    A single INDI client connection to one INDI server (e.g. a local
    `indiserver` running `indi_lx200am5` for the AM3/AM5).

    Devices are represented to callers as plain device-name strings (not
    objects) — everything needed is looked up internally by (device, prop)
    key. Not thread-safe for concurrent goto calls — callers (mount.py)
    already serialize hardware access through a single-worker
    ThreadPoolExecutor, so this class assumes one blocking call at a time.
    """

    def __init__(self, host: str, port: int = DEFAULT_INDI_PORT) -> None:
        self.host = host
        self.port = port
        self._sock: socket.socket | None = None
        self._reader_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._send_lock = threading.Lock()

        self._lock = threading.RLock()
        # device -> prop_name -> {"type": "Number"|"Switch"|"Text"|"Light",
        #                          "state": str, "elements": {elem_name: value}}
        self._devices: dict[str, dict[str, dict]] = {}
        self._prop_events: dict[tuple[str, str], threading.Event] = {}

        self._stream = _XmlStream(self._on_element)

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def connect(self, discover_s: float = 3.0) -> None:
        try:
            sock = socket.create_connection((self.host, self.port), timeout=10.0)
        except OSError as e:
            raise RuntimeError(
                f"Could not reach INDI server at {self.host}:{self.port} — "
                "check indiserver is running there and reachable on the "
                "network."
            ) from e
        sock.settimeout(None)
        self._sock = sock
        self._stop.clear()
        self._reader_thread = threading.Thread(
            target=self._reader_loop, daemon=True, name=f"indi-reader-{self.host}"
        )
        self._reader_thread.start()
        self._send('<getProperties version="1.7"/>')
        logger.info("INDI: connected to %s:%d, discovering devices…", self.host, self.port)
        time.sleep(discover_s)   # let defXXXVector messages arrive

    def disconnect(self) -> None:
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        if self._reader_thread is not None:
            self._reader_thread.join(timeout=2.0)
            self._reader_thread = None

    def _reader_loop(self) -> None:
        sock = self._sock
        try:
            while not self._stop.is_set():
                try:
                    data = sock.recv(_RECV_CHUNK)
                except OSError:
                    break
                if not data:
                    break
                try:
                    self._stream.feed(data)
                except xml.parsers.expat.ExpatError as e:
                    logger.warning("INDI: XML parse error, resyncing: %s", e)
                    self._stream = _XmlStream(self._on_element)
        finally:
            logger.info("INDI: reader thread for %s:%d exiting", self.host, self.port)

    def _send(self, xml_fragment: str) -> None:
        if self._sock is None:
            raise RuntimeError("Not connected to INDI server")
        data = (xml_fragment + "\n").encode("utf-8")
        with self._send_lock:
            self._sock.sendall(data)

    # ------------------------------------------------------------------
    # Incoming message handling
    # ------------------------------------------------------------------

    def _on_element(self, node: dict) -> None:
        tag = node["tag"]
        attrs = node["attrs"]
        logger.debug("INDI: recv <%s device=%r name=%r>", tag, attrs.get("device"), attrs.get("name"))

        if tag == "message":
            text = attrs.get("message", "")
            if text:
                logger.info("INDI message [%s]: %s", attrs.get("device", "server"), text)
            return

        if tag.startswith("del"):
            device = attrs.get("device")
            name = attrs.get("name")
            with self._lock:
                if device in self._devices:
                    if name:
                        self._devices[device].pop(name, None)
                    else:
                        self._devices.pop(device, None)
            return

        kind = next((k for k in _VECTOR_KINDS if tag in (f"def{k}Vector", f"set{k}Vector")), None)
        if kind is None:
            return   # not a vector message we care about

        device = attrs.get("device", "")
        name = attrs.get("name", "")
        if not device or not name:
            return
        state = attrs.get("state", "Idle")
        is_def = tag.startswith("def")
        child_prefix = "def" if is_def else "one"

        elements: dict[str, object] = {}
        for child in node["children"]:
            if not child["tag"].startswith(child_prefix):
                continue
            elem_name = child["attrs"].get("name")
            if elem_name is None:
                continue
            if kind == "Number":
                try:
                    elements[elem_name] = _parse_number(child["text"])
                except ValueError:
                    logger.debug("INDI: unparseable number %r for %s.%s.%s",
                                 child["text"], device, name, elem_name)
            elif kind == "Switch":
                elements[elem_name] = child["text"].strip() == "On"
            elif kind == "Text":
                elements[elem_name] = child["text"].strip()

        with self._lock:
            dev = self._devices.setdefault(device, {})
            prop = dev.setdefault(name, {"type": kind, "state": state, "elements": {}, "version": 0})
            prop["state"] = state
            prop["elements"].update(elements)
            prop["version"] += 1
            ev = self._prop_events.setdefault((device, name), threading.Event())
        ev.set()

    # ------------------------------------------------------------------
    # Device discovery (by DRIVER_INFO.DRIVER_INTERFACE bitmask, not name)
    # ------------------------------------------------------------------

    def find_device(self, interface_flag: int, label: str, timeout: float = _PROPERTY_TIMEOUT_S) -> str:
        """Return the name of the first known device exposing the given interface bit."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                for dev_name, props in self._devices.items():
                    info = props.get("DRIVER_INFO")
                    if info:
                        bitmask = int(info["elements"].get("DRIVER_INTERFACE") or 0)
                        if bitmask & interface_flag:
                            return dev_name
            time.sleep(0.2)
        with self._lock:
            seen = list(self._devices.keys())
        raise RuntimeError(
            f"No {label} device found on INDI server {self.host}:{self.port} within {timeout}s. "
            f"Devices seen: {seen}"
        )

    def find_telescope(self) -> str:
        return self.find_device(TELESCOPE_INTERFACE, "telescope")

    # ------------------------------------------------------------------
    # Device connect (CONNECTION switch)
    # ------------------------------------------------------------------

    def connect_device(self, device: str, timeout: float = _PROPERTY_TIMEOUT_S) -> None:
        with self._lock:
            already = self._devices.get(device, {}).get("CONNECTION", {}).get("elements", {}).get("CONNECT")
        if already:
            return
        self.send_switch(device, "CONNECTION", "CONNECT")
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                if self._devices.get(device, {}).get("CONNECTION", {}).get("elements", {}).get("CONNECT"):
                    return
            time.sleep(0.2)
        raise RuntimeError(f"INDI device {device!r} did not report connected within {timeout}s")

    def disconnect_device(self, device: str) -> None:
        try:
            self.send_switch(device, "CONNECTION", "DISCONNECT")
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Property wait / read / write helpers
    # ------------------------------------------------------------------

    def _wait_property(self, device: str, prop_name: str, timeout: float = _PROPERTY_TIMEOUT_S) -> dict:
        key = (device, prop_name)
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                prop = self._devices.get(device, {}).get(prop_name)
                if prop is not None:
                    return prop
                ev = self._prop_events.setdefault(key, threading.Event())
            if ev.wait(0.5):
                ev.clear()
        raise RuntimeError(f"INDI property {prop_name!r} not available on {device!r}")

    def wait_property(self, device: str, prop_name: str, timeout: float = _PROPERTY_TIMEOUT_S) -> dict:
        """Block until `prop_name` is defined for `device` — needed after
        switching CONNECTION_MODE, which dynamically defines/undefines
        DEVICE_PORT depending on serial vs. TCP mode."""
        return self._wait_property(device, prop_name, timeout)

    def get_number(self, device: str, prop_name: str) -> dict:
        self._send(f'<getProperties version="1.7" device={_q(device)} name={_q(prop_name)}/>')
        prop = self._wait_property(device, prop_name)
        with self._lock:
            return dict(prop["elements"])

    def send_number(self, device: str, prop_name: str, values: dict) -> None:
        body = "".join(
            f'<oneNumber name={_q(str(k))}>{float(v):.10f}</oneNumber>'
            for k, v in values.items()
        )
        self._send(f'<newNumberVector device={_q(device)} name={_q(prop_name)}>{body}</newNumberVector>')

    def send_text(self, device: str, prop_name: str, values: dict) -> None:
        body = "".join(
            f'<oneText name={_q(str(k))}>{_esc(str(v))}</oneText>'
            for k, v in values.items()
        )
        self._send(f'<newTextVector device={_q(device)} name={_q(prop_name)}>{body}</newTextVector>')

    def send_switch(self, device: str, prop_name: str, on_element: str) -> None:
        body = f'<oneSwitch name={_q(on_element)}>On</oneSwitch>'
        self._send(f'<newSwitchVector device={_q(device)} name={_q(prop_name)}>{body}</newSwitchVector>')

    def send_number_and_wait(self, device: str, prop_name: str, values: dict, timeout: float) -> None:
        """
        Send a numberVector and block until the server's *next* update for
        this property (not a stale cached one — see below) reports a
        non-Busy state, e.g. waiting out a slew or exposure.

        A naive "send, then poll until state != Busy" is racy: the property
        is very likely still sitting at whatever non-Busy state it had
        *before* this command (e.g. "Ok" from the last idle report), and a
        poll immediately after send() can observe that stale value and
        return before the server has even processed the command — the
        caller believes the slew finished when it hasn't started. Recording
        the property's version counter before sending and requiring it to
        advance at least once closes that race.
        """
        key = (device, prop_name)
        with self._lock:
            prop = self._devices.get(device, {}).get(prop_name)
            baseline_version = prop["version"] if prop is not None else 0
        self.send_number(device, prop_name, values)
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                prop = self._devices.get(device, {}).get(prop_name)
                if prop is not None and prop["version"] > baseline_version and prop["state"] != "Busy":
                    return
                ev = self._prop_events.setdefault(key, threading.Event())
            if ev.wait(0.5):
                ev.clear()
        raise TimeoutError(f"INDI property {prop_name!r} on {device!r} still busy after {timeout}s")
