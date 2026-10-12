"""Device sessions: glue :mod:`atksoft.protocol` onto :mod:`atksoft.hidraw`.

:mod:`protocol` knows what the bytes mean, :mod:`hidraw` knows how to move
them.  This module is the missing middle: it finds the one hidraw node that
carries the configuration reports, performs the firmware's read handshake and
makes the codecs line up with what the device actually declares.

Two facts drive the whole design and were both confirmed against the mouse's
own report descriptor (interface 2 of the dongle):

*   **The node is not always the same.**  The dongle exposes four HID
    interfaces and only one answers the vendor protocol.  ``/dev/hidrawN`` is
    not stable across replugs either, so the node is located by inspecting the
    report descriptor, never by remembering a path.

*   **The vendor buffer sizes are not the transfer sizes.**  ``build()``
    produces the vendor's own packet sizes (56 bytes for report 0x04, 15 for
    0x05, ...) whose byte 1 is a packet-length *field*, while the descriptor
    caps the transfer at 52 and 13 bytes.  The checksum always falls inside the
    shorter frame, so the session truncates on write and zero-pads on read.
    Sending the longer frame is what makes the control endpoint stall (EPIPE).
"""

from __future__ import annotations

import time

from . import protocol as p
from .hidraw import HidrawDevice, HidrawError, HidrawNode, enumerate_hidraw

__all__ = [
    "SessionError",
    "SUPPORTED_PIDS",
    "CONFIG_MARKERS",
    "REPORT_NAMES",
    "CODEC_LENGTHS",
    "list_nodes",
    "is_config_node",
    "find_config_node",
    "MouseSession",
    "open_session",
]

#: Product ids whose dongle (or cable) carries the vendor configuration reports.
SUPPORTED_PIDS = (p.PID_DONGLE, p.PID_WIRED)

#: A node is the configuration interface when it exposes both of these: the DPI
#: report (0x04) and the read-permission report (0xA0).  The other three
#: interfaces only ever carry keyboard/mouse input reports.
CONFIG_MARKERS = (p.RID_DPI, p.RID_READ)

#: Names used by ``atksoft list`` and ``atksoft raw``.
REPORT_NAMES = {
    p.RID_DPI: "dpi",
    p.RID_LIGHT: "lighting",
    p.RID_POLL: "polling",
    p.RID_WAKE: "wake-up mode",
    p.RID_BUTTONS: "button mapping",
    p.RID_MACRO: "macro",
    p.RID_PROFILE: "profile blob",
    p.RID_VERSION: "version",
    p.RID_PROFILE_SET: "profile count",
    p.RID_READ: "read grant",
}

#: Lengths the codecs in :mod:`protocol` expect, including the report-id byte.
#: A read that comes back shorter than this cannot be parsed.
CODEC_LENGTHS = {
    p.RID_DPI: p.DPI_LENGTH,
    p.RID_LIGHT: p.LIGHT_LENGTH,
    p.RID_POLL: p.POLL_LENGTH,
    p.RID_BUTTONS: p.BUTTON_LENGTH,
    p.RID_MACRO: p.MACRO_LENGTH,
    p.RID_WAKE: p.WAKE_LENGTH,
    p.RID_VERSION: p.VERSION_LENGTH,
    p.RID_PROFILE: p.PROFILE_LENGTH,
    p.RID_PROFILE_SET: p.PROFILE_SET_LENGTH,
    # The read-permission report is part of the handshake, not a codec, but it
    # needs a length too: without this a descriptor that fails to declare 0xA0
    # would make every single read fail with a confusing "unknown length".
    p.RID_READ: p.GRANT_LENGTH,
}


class SessionError(RuntimeError):
    """No usable mouse, or the firmware refused to cooperate."""


# ------------------------------------------------------------ node discovery

def list_nodes(pids=SUPPORTED_PIDS) -> list:
    """Every hidraw node belonging to a supported mouse, config interface first."""
    nodes = [node for node in enumerate_hidraw()
             if node.vid == p.VENDOR_ID and node.pid in tuple(pids)]
    return sorted(nodes, key=lambda node: (not is_config_node(node), node.path))


def is_config_node(node: HidrawNode) -> bool:
    """True when ``node`` exposes the vendor configuration feature reports."""
    return all(rid in node.features for rid in CONFIG_MARKERS)


def find_config_node(pids=SUPPORTED_PIDS, node: str | None = None) -> HidrawNode:
    """Return the node that carries the configuration reports.

    ``node`` pins the search to one path (``--node /dev/hidraw2``).  Raises
    :class:`SessionError` with the candidate list when nothing matches, because
    "mouse not plugged in" and "wrong interface" need different fixes.
    """
    nodes = list_nodes(pids)
    if node:
        wanted = [candidate for candidate in nodes if candidate.path == node]
        if not wanted:
            present = ", ".join(candidate.path for candidate in nodes) or "none"
            raise SessionError(f"{node} is not a supported mouse ({p.VENDOR_ID:04x}:"
                               f"dongle {p.PID_DONGLE:04x} / cable {p.PID_WIRED:04x}); "
                               f"found: {present}")
        nodes = wanted
    for candidate in nodes:
        if is_config_node(candidate):
            return candidate
    if not nodes:
        raise SessionError(
            f"no Attack Shark mouse found (looking for {p.VENDOR_ID:04x}:"
            f"{p.PID_DONGLE:04x} or {p.VENDOR_ID:04x}:{p.PID_WIRED:04x}) -- "
            "plug in the dongle or the USB-C cable")
    raise SessionError(
        "no configuration interface found -- this link is not the config "
        "channel; plug the dongle in directly rather than through a hub "
        f"(nodes seen: {', '.join(candidate.path for candidate in nodes)})")


def _transfer_length(node: HidrawNode, rid: int) -> int:
    """Transfer size for ``rid``: descriptor payload + 1 report-id byte."""
    payload = node.features.get(rid)
    if payload is None:
        known = CODEC_LENGTHS.get(rid)
        if known is None:
            raise SessionError(f"{node.path} does not expose feature report {rid:#04x}")
        return known
    return payload + 1


def _fit(buf, length: int) -> bytearray:
    """Pad ``buf`` with zeros (or drop the vendor's padding) to ``length``."""
    raw = bytearray(bytes(buf)[:length])
    if len(raw) < length:
        raw.extend(b"\x00" * (length - len(raw)))
    return raw


def _same_frame(written, answered) -> bool:
    """True when a frame we are about to write matches the device's last answer.

    Byte[1] is deliberately ignored.  On a read the firmware puts the *declared
    read length + 2* there, while a write frame carries the vendor's packet
    length, so the two never agree byte for byte even when every field does --
    without this, ``apply()`` would rewrite all five reports on every call.
    Everything else, checksums included, has to match exactly.

    Both arguments may be ``None``; ``answered`` is only ever passed in from a
    read, and ``None`` means "never read this report", never a match.
    """
    if written is None or answered is None:
        return False
    mine, theirs = bytes(written), bytes(answered)
    if len(mine) != len(theirs):
        return False
    return mine[:1] == theirs[:1] and mine[2:] == theirs[2:]


# ------------------------------------------------------------------- session

class MouseSession:
    """One open connection to a mouse's configuration interface.

    Use it as a context manager::

        with MouseSession() as mouse:
            print(mouse.read_dpi())

    ``node`` is located automatically.  ``device`` exists for tests: pass any
    object with ``open``/``close``/``set_feature``/``get_feature``.
    """

    def __init__(self, node: HidrawNode | None = None, *, device=None,
                 retries: int = 3, grant_delay: float = p.GRANT_DELAY_S,
                 settle: float = 0.1):
        self.node = node or find_config_node()
        self.device = device if device is not None else HidrawDevice(self.node.path)
        self.retries = retries
        self.grant_delay = grant_delay
        self.settle = settle
        #: Report id -> bytes as the device last sent them (read-modify-write).
        self.snapshot: dict = {}
        #: Bytes the kernel handed back by the last read, when it reports it.
        self.last_read: int | None = None

    # -- lifetime ---------------------------------------------------------

    def __enter__(self) -> "MouseSession":
        self.device.open()
        return self

    def __exit__(self, *exc_info) -> None:
        self.device.close()

    @property
    def path(self) -> str:
        return self.node.path

    # -- geometry ---------------------------------------------------------

    def supports(self, rid: int) -> bool:
        """True when this interface exposes feature report ``rid``."""
        return rid in self.node.features

    def transfer_length(self, rid: int) -> int:
        """Bytes to move for ``rid``.

        The report descriptor is authoritative, but it is not the only source:
        the fitting rule lives in :func:`_transfer_length`.  Reports the
        descriptor does not declare fall back to the vendor codec's size, which
        is what the firmware itself sends.
        """
        if rid in self.node.features:
            return _transfer_length(self.node, rid)
        fallback = self.codec_length(rid)
        if fallback is None:
            raise SessionError(
                f"report {rid:#04x} is not in {self.path}'s descriptor and has "
                f"no known vendor length")
        return fallback

    def codec_length(self, rid: int) -> int | None:
        """Bytes the matching codec in :mod:`protocol` expects, if it has one."""
        return CODEC_LENGTHS.get(rid)

    def support_report(self) -> str:
        """The descriptor's feature-report inventory, for ``atksoft list``."""
        return " ".join(f"{rid:02x}:{length + 1}"
                        for rid, length in sorted(self.node.features.items()))

    # -- raw transport ----------------------------------------------------

    def frame_for(self, report, *, wired: bool = False) -> bytearray:
        """The exact bytes :meth:`write_report` would put on the wire.

        Shared by ``--dry-run``, by :meth:`apply` and by the GUI's preview: it
        answers "what will this setting look like once the vendor's padding has
        been trimmed to the descriptor's transfer size?" without sending
        anything.
        """
        raw = bytes(report.build(wired=wired)) if isinstance(report, p.ProfileSettings) \
            else bytes(report.build())
        return _fit(raw, self.transfer_length(raw[0]))

    def write_report(self, payload) -> bytearray:
        """Send one feature report.

        The buffer is trimmed to the descriptor's transfer size (that is what
        keeps the control endpoint alive) and returned so callers can log the
        exact frame that went out.
        """
        raw = bytes(payload)
        if not raw:
            raise SessionError("refusing to write an empty report")
        rid = raw[0]
        if not self.supports(rid):
            raise SessionError(f"{self.path} does not accept writes to report {rid:#04x}")
        frame = _fit(raw, self.transfer_length(rid))
        self.device.set_feature(frame)
        self.snapshot[rid] = bytes(frame)
        return frame

    def read_report(self, rid: int, *, declared_length: int | None = None,
                    parameter: int | None = None, retries: int | None = None) -> bytearray:
        """Ask for permission to read ``rid``, then read it.

        Returns the raw transfer bytes (descriptor length), *not* padded to the
        codec's size -- use the ``read_*`` helpers below for that.

        The answer is checked against the length the grant asked for (byte[1] of
        a reply echoes it + 2); see :meth:`_settled_frame` for why that check has
        to exist at all.
        """
        if not self.supports(rid):
            raise SessionError(f"{self.path} does not expose feature report {rid:#04x}")
        transfer = self.transfer_length(rid)
        if declared_length is None:
            # The vendor's declared length is what the firmware is prepared to
            # emit; it is not the descriptor's size (0x83 vs 0x40 for macros).
            declared_length = p.READ_GRANT_LENGTH.get(rid, transfer)
        if parameter is None:
            # A report may need a particular byte[4] to answer in the layout its
            # codec expects (0x0C shifts its fields one byte right when the
            # parameter is non-zero); see GRANT_PARAMETER_DEFAULT.
            parameter = p.GRANT_PARAMETER_DEFAULT.get(rid, p.GRANT_DEFAULT_PARAMETER)
        grant = p.build_grant_request(rid, parameter, declared_length)
        attempts = self.retries if retries is None else retries
        # The echo is only held against the report when READ_GRANT_LENGTH states
        # the length that was asked for: that table is all reports the vendor's
        # own reader handles, and the echo is confirmed on the live dongle for
        # every one of them.  For the rest it would be an assumption.
        strict = rid in p.READ_GRANT_LENGTH
        last = "no answer"
        for attempt in range(attempts + 1):
            try:
                if self.settle:
                    time.sleep(self.settle)
                self.device.set_feature(grant)
                time.sleep(self.grant_delay)
                status = self.device.get_feature(p.RID_READ, self.transfer_length(p.RID_READ))
                if not p.parse_grant_response(status):
                    last = (f"the dongle refused the read request, answering with "
                            f"{status[1]:#04x}")
                else:
                    frame, echoed = self._settled_frame(rid, transfer, declared_length)
                    if not strict or echoed == declared_length + 2:
                        self.last_read = getattr(self.device, "last_transfer", None)
                        self.snapshot[rid] = bytes(frame)
                        return frame
                    last = (f"the buffer still held a {echoed}-byte answer, not the "
                            f"{declared_length + 2}-byte one this grant asked for")
            except (HidrawError, p.ProtocolError, OSError) as exc:
                last = str(exc)
            time.sleep(0.3 * (attempt + 1))
        raise SessionError(
            f"could not read feature report {rid:#04x} ({last}) in {attempts + 1} "
            f"attempt(s) -- the link may not have a live connection to the mouse "
            f"right now (keep the mouse switched on; the dongle only answers while "
            f"it is paired)")

    def _settled_frame(self, rid: int, transfer: int,
                       declared_length: int, tries: int = 3):
        """Read ``rid`` until its buffer holds *this* answer, not leftovers.

        The firmware grants a read first and fills the report afterwards, so the
        first ``GET_FEATURE`` after a grant can still return whatever its buffer
        held: the last frame written to some other report, or the answer to an
        earlier request.  Byte[1] is what exposes it -- a reply echoes the
        declared read length + 2 (:func:`protocol.reply_length`), so a buffer
        still holding the answer to an *earlier, shorter* grant of this same
        report is the one case the report id alone cannot catch.

        Nothing is sent here, so a poll costs one ioctl: hand the frame and its
        echo back to :meth:`read_report` to judge, and let it re-grant instead
        when the buffer never agrees.
        """
        frame = bytes(self.device.get_feature(rid, transfer))
        wanted = declared_length + 2
        for _ in range(max(tries - 1, 0)):
            echoed = p.reply_length(frame)
            if echoed is None or echoed == wanted:
                break
            time.sleep(self.grant_delay)
            frame = bytes(self.device.get_feature(rid, transfer))
        return frame, p.reply_length(frame)

    # -- codecs -----------------------------------------------------------

    def _padded(self, rid: int, frame) -> bytearray:
        """Pad a transfer frame to the size the matching codec insists on."""
        needed = self.codec_length(rid) or len(frame)
        return _fit(frame, needed)

    def read_dpi(self, profile: int = 1, verify: bool = True) -> p.DpiSettings:
        """Report 0x04 as sent (52 bytes) padded to the codec's 56."""
        return p.DpiSettings.from_bytes(
            self._padded(p.RID_DPI, self.read_report(p.RID_DPI, parameter=profile)),
            verify=verify)

    def read_lighting(self, verify: bool = True) -> p.LightingSettings:
        """Report 0x05 as sent (13 bytes) padded to the codec's 15."""
        return p.LightingSettings.from_bytes(
            self._padded(p.RID_LIGHT, self.read_report(p.RID_LIGHT)), verify=verify)

    def read_polling(self) -> p.PollingRate:
        """Report 0x06 (9 bytes, matching the codec exactly)."""
        return p.PollingRate.from_bytes(
            self._padded(p.RID_POLL, self.read_report(p.RID_POLL)))

    def read_buttons(self, profile: int = 1, verify: bool = True) -> p.ButtonSettings:
        """Report 0x08 (59 bytes, matching the codec exactly)."""
        return p.ButtonSettings.from_bytes(
            self._padded(p.RID_BUTTONS, self.read_report(p.RID_BUTTONS, parameter=profile)),
            verify=verify)

    def read_profile_settings(self, verify: bool = True) -> p.ProfileSettings:
        """Report 0x0C: which profile is live and how many exist."""
        return p.ProfileSettings.from_bytes(
            self._padded(p.RID_PROFILE_SET, self.read_report(p.RID_PROFILE_SET)),
            verify=verify)

    def read_version(self) -> p.RawReport:
        """Report 0x0B, kept verbatim (the vendor publishes no layout for it)."""
        return p.parse_version(
            self._padded(p.RID_VERSION, self.read_report(p.RID_VERSION)))

    def read_profile_blob(self) -> p.RawReport:
        """Report 0x0A, kept verbatim (opaque 128-byte profile record)."""
        return p.parse_profile_blob(
            self._padded(p.RID_PROFILE, self.read_report(p.RID_PROFILE)))

    def read_wake(self) -> p.RawReport:
        """Report 0x07, kept verbatim -- see ``protocol.WAKE_UP_MODE_NOTES``."""
        return p.parse_wake_report(
            self._padded(p.RID_WAKE, self.read_report(p.RID_WAKE)))

    def read_macro(self, macro_id: int = 1, verify: bool = True) -> p.Macro:
        """Read one macro.

        The interface declares a 64-byte feature report for 0x09 while the
        vendor's reader expects 131 bytes (checksum at offset 129), so this
        says so plainly instead of parsing zeros as data.
        """
        frame = self.read_report(p.RID_MACRO, parameter=macro_id)
        if self.last_read is not None and 0 < self.last_read < len(frame):
            frame = bytearray(frame[:self.last_read])
        if len(frame) < p.MACRO_LENGTH:
            raise SessionError(
                f"the macro report came back as {len(frame)} of {p.MACRO_LENGTH} "
                f"bytes -- this interface declares a "
                f"{self.transfer_length(p.RID_MACRO)}-byte report for 0x09, so a "
                f"full macro cannot be fetched over hidraw "
                f"(see protocol.MACRO_WRITE_NOTES)")
        return p.Macro.from_bytes(frame, verify=verify)

    # -- writes -----------------------------------------------------------

    def write_dpi(self, settings: p.DpiSettings) -> bytearray:
        """Write report 0x04 (56-byte vendor buffer trimmed to the wire's 52)."""
        return self.write_report(settings.build())

    def write_lighting(self, settings: p.LightingSettings) -> bytearray:
        """Write report 0x05 (15-byte vendor buffer trimmed to the wire's 13)."""
        return self.write_report(settings.build())

    def write_polling(self, settings: p.PollingRate) -> bytearray:
        """Write report 0x06 (write-only in practice, like the vendor tool)."""
        return self.write_report(settings.build())

    def write_buttons(self, settings: p.ButtonSettings) -> bytearray:
        """Write report 0x08 (59 bytes, matching the descriptor)."""
        return self.write_report(settings.build())

    def write_profile_settings(self, settings: p.ProfileSettings,
                               wired: bool = False) -> bytearray:
        """Write report 0x0C; ``wired`` sends the 6-byte cable variant."""
        return self.write_report(settings.build(wired=wired))

    def write_macro(self, macro: p.Macro) -> list:
        """Write a macro as its three 64-byte pages (still experimental)."""
        return [self.write_report(page) for page in p.build_macro_pages(macro)]

    # -- whole-report convenience ----------------------------------------

    #: key -> report id: the reports the settings registry edits.
    SETTINGS_REPORTS = (
        ("dpi", p.RID_DPI),
        ("lighting", p.RID_LIGHT),
        ("polling", p.RID_POLL),
        ("buttons", p.RID_BUTTONS),
        ("profile", p.RID_PROFILE_SET),
    )

    def _readers(self, profile: int = 1) -> dict:
        """``key -> zero-argument reader`` for every report the registry edits."""
        return {
            "dpi": lambda: self.read_dpi(profile=profile),
            "lighting": self.read_lighting,
            "polling": self.read_polling,
            "buttons": lambda: self.read_buttons(profile=profile),
            "profile": self.read_profile_settings,
        }

    def read_settings(self, profile: int = 1, keys=None) -> dict:
        """Read the reports the settings registry edits.

        Only reports this interface actually exposes come back, so a caller can
        never mistake "not present" for a zero value.
        """
        state: dict = {}
        readers = self._readers(profile)
        for key, rid in self.SETTINGS_REPORTS:
            if self.supports(rid) and (keys is None or key in keys):
                state[key] = readers[key]()
        return state

    def apply(self, state: dict, keys=None, verify: bool = True) -> list:
        """Write back every report in ``state`` whose bytes actually changed.

        Reports are skipped when they still match what the device last handed
        over (``self.snapshot``), so a read-modify-write cycle touches only what
        the user changed.  With ``verify`` (the default) each written report is
        read back and returned alongside its report id, because the firmware
        normalises some fields rather than storing bytes verbatim.

        Report 0x05 is the one exception to "only what changed": it follows every
        DPI write, because report 0x04 only *flashes* the LED (byte 49) and hands
        it straight back to 0x05, which owns the steady colour.  See
        :meth:`_repeat_lighting`.
        """
        writers = {
            "dpi": self.write_dpi,
            "lighting": self.write_lighting,
            "polling": self.write_polling,
            "buttons": self.write_buttons,
            "profile": self.write_profile_settings,
        }
        results = []
        written = set()
        for key, rid in self.SETTINGS_REPORTS:
            report = state.get(key)
            if report is None or (keys is not None and key not in keys):
                continue
            if self.snapshot.get(rid) is not None and \
                    _same_frame(self.frame_for(report), self.snapshot[rid]):
                continue
            writers[key](report)
            written.add(rid)
            fresh = None
            if verify:
                try:
                    fresh = self._readers()[key]()
                except (SessionError, p.ProtocolError):
                    fresh = None
            results.append((rid, fresh))
        if p.RID_DPI in written and p.RID_LIGHT not in written and self.supports(p.RID_LIGHT):
            # The caller asked for lighting but the bytes matched, so it was
            # skipped above; echoing it back is still the point of the repeat.
            lighting = state.get("lighting") if (keys is None or "lighting" in keys) else None
            results.append(self._repeat_lighting(lighting, verify))
        return results

    def _repeat_lighting(self, settings, verify: bool):
        """Send report 0x05 again after a DPI write so the LED re-renders.

        Report 0x04 carries the stage colours and the active stage, but its byte
        49 is a one-shot animation: the firmware plays it and then returns the
        LED to whatever report 0x05 last asked for.  Writing 0x04 alone therefore
        leaves the LED on the colour of the stage it was already showing, which is
        what a changed active stage (or a changed stage colour) looks like it did
        nothing -- and why both vendor drivers write the two reports as a pair, in
        this order, on every apply.

        ``settings`` is the lighting report the caller handed over, and is re-read
        from the device when the caller only passed the DPI report (that is what
        ``atksoft set dpi`` and the GUI's DPI tab do).
        """
        if settings is None:
            settings = self.read_lighting()
        self.write_lighting(settings)
        fresh = None
        if verify:
            try:
                fresh = self.read_lighting()
            except (SessionError, p.ProtocolError):
                fresh = None
        return (p.RID_LIGHT, fresh)

    # -- interrupt-IN events ---------------------------------------------

    def events(self, timeout: float = 2.0) -> list:
        """Decode every interrupt-IN event seen within ``timeout`` seconds."""
        found = []
        end = time.monotonic() + max(0.0, timeout)
        while True:
            remaining = end - time.monotonic()
            if remaining <= 0 or not self.device.wait_readable(remaining):
                break
            data = self.device.read(p.EVENT_LENGTH)
            if not data:
                continue
            if p.is_event_report(data):
                try:
                    found.append(p.decode_event(data))
                except p.ProtocolError:
                    continue
        return found

    def battery(self, timeout: float = 2.5, attempts: int = 3):
        """The mouse's battery level, or ``None`` when no event arrives.

        Only a wireless link reports one, and the first battery event after
        connecting is frequently a stale 100%, so the last event of the window
        wins.  ``attempts`` re-opens the window that many times, because the
        event only arrives when the mouse has something to say.
        """
        for attempt in range(1, max(1, attempts) + 1):
            levels = [event for event in self.events(timeout)
                      if isinstance(event, p.BatteryEvent)]
            if levels:
                return levels[-1]
            if attempt < attempts:
                time.sleep(0.4)
        return None

    def watch(self, seconds: float | None = None, timeout: float = 1.0):
        """Yield events as they arrive; ``seconds=None`` runs until interrupted."""
        end = None if seconds is None else time.monotonic() + seconds
        while end is None or time.monotonic() < end:
            for event in self.events(timeout):
                yield event


def open_session(node: str | None = None, **kwargs) -> MouseSession:
    """Open the configuration interface of the attached Attack Shark mouse."""
    return MouseSession(find_config_node(node=node), **kwargs)
