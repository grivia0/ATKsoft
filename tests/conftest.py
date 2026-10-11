"""Shared test hardware.

The suite never touches a real mouse: :class:`FakeDevice` stands in for
:class:`atksoft.hidraw.HidrawDevice` (it records ``SET_FEATURE`` buffers and
replays ``GET_FEATURE`` from a table) and :class:`FakeNode` is a
:class:`atksoft.hidraw.HidrawNode` with nothing but the two attributes
``MouseSession`` reads off it.

The descriptor lengths are the ones the live X6 dongle reports for its
configuration interface (interface 2), as feature *payload* sizes excluding the
report-id byte::

    04:51  05:12  06:8  07:7  08:58  09:63  0a:127  0b:7  0c:5  a0:7

The mismatch that matters is 0x04/0x05: ``protocol`` builds the vendor's packet
sizes (56 and 15 bytes) while the wire only moves 52 and 13, so the session has
to trim on the way out and zero-pad on the way in.
"""

from __future__ import annotations

import dataclasses

import pytest

from atksoft import protocol as p
from atksoft import session as s

#: What the dongle's configuration interface declares (payload bytes).
FEATURES = {0x04: 51, 0x05: 12, 0x06: 8, 0x07: 7, 0x08: 58,
            0x09: 63, 0x0A: 127, 0x0B: 7, 0x0C: 5, 0xA0: 7}

#: key -> (report id, codec, writer, reader)
TRIPLES = (
    ("dpi", p.RID_DPI, p.DpiSettings, "write_dpi", "read_dpi"),
    ("lighting", p.RID_LIGHT, p.LightingSettings, "write_lighting", "read_lighting"),
    ("polling", p.RID_POLL, p.PollingRate, "write_polling", "read_polling"),
    ("buttons", p.RID_BUTTONS, p.ButtonSettings, "write_buttons", "read_buttons"),
    ("profile", p.RID_PROFILE_SET, p.ProfileSettings, "write_profile_settings",
     "read_profile_settings"),
)


class FakeNode:
    """The attributes :class:`MouseSession` reads off a ``HidrawNode``."""

    def __init__(self, features=None, path="/dev/hidraw_fake",
                 vid=p.VENDOR_ID, pid=p.PID_DONGLE, name="Fake Mouse",
                 interface=2):
        self.path = path
        self.features = dict(FEATURES if features is None else features)
        self.vid = vid
        self.pid = pid
        self.name = name
        self.interface = interface

    def transfer_len(self, rid: int) -> int:
        return self.features[rid] + 1


class FakeDevice:
    """A hidraw device that answers from a table and records every write."""

    def __init__(self, replies=None, grant: bool = True, stale=None):
        self.replies = dict(replies or {})      # report id -> bytes the firmware sends
        self.grant = grant
        self.stale = dict(stale or {})          # rid -> [frames] the buffer "still holds"
        self.writes = []                        # every SET_FEATURE buffer, in order
        self.queue = []                         # pending interrupt-IN reports
        self.last_transfer = None               # what the kernel "moved"
        self.granted = (None, None)             # (report id, declared length) asked for
        self.opened = False

    # -- lifetime
    def open(self):
        self.opened = True
        return self

    def close(self):
        self.opened = False

    # -- feature reports
    def set_feature(self, payload) -> None:
        raw = bytes(payload)
        self.writes.append(raw)
        if len(raw) >= 3 and raw[0] == p.RID_READ:
            self.granted = (raw[1], raw[2])     # rid, declared length

    def get_feature(self, rid: int, transfer_len: int) -> bytearray:
        self.last_transfer = transfer_len
        if rid == p.RID_READ:
            status = p.GRANT_GRANTED if self.grant else p.GRANT_REFUSED
            return bytearray([p.RID_READ, status] + [0] * (transfer_len - 2))
        if self.stale.get(rid):
            # The firmware grants a read first and fills the buffer afterwards, so
            # the first GET_FEATURE can hand over an answer the dongle has not
            # replaced yet -- with the *older* grant's length echoing in byte[1].
            # Each scripted frame is served once.
            return bytearray(self.stale[rid].pop(0))
        data = bytes(self.replies.get(rid, b""))[:transfer_len]
        buf = bytearray(transfer_len)
        buf[:len(data)] = data
        buf[0] = rid
        if len(buf) > 1 and self.granted[0] == rid:
            # The answer echoes the length it was asked for in byte[1]: declared
            # length + 2, the id and length bytes included.  Confirmed on the live
            # dongle for every report it exposes, and *not* the vendor's packet
            # length that a write frame carries in that byte -- which is why a
            # reply never equals a write frame and ``session._same_frame``
            # compares everything except byte[1].
            buf[1] = (self.granted[1] + 2) & 0xFF
        return buf

    # -- interrupt IN
    def read(self, size: int):
        return self.queue.pop(0) if self.queue else None

    def wait_readable(self, timeout: float) -> bool:
        return bool(self.queue)


# --------------------------------------------------------------- helpers

def build(codec, key: str, **kwargs) -> bytes:
    """Serialise ``codec`` the way the matching ``write_*`` helper would."""
    return bytes(codec.build(wired=kwargs.get("wired", False)) if key == "profile"
                 else codec.build())


def default_replies() -> dict:
    """A valid answer for every report the default descriptor declares.

    Each codec answers with its own default frame trimmed to the wire length,
    so ``read_settings()`` has something parseable for all five reports.
    """
    replies = {}
    for key, rid, codec, _writer, _reader in TRIPLES:
        try:
            codec = codec()
        except TypeError:                   # needs arguments that have no default
            continue
        replies[rid] = build(codec, key)[:FEATURES[rid] + 1]
    return replies


def make_mouse(features=None, replies=None, grant: bool = True, stale=None,
               vid=p.VENDOR_ID, pid=p.PID_DONGLE, **kwargs):
    """``(MouseSession, FakeDevice)`` with the settle/grant delays switched off."""
    node = FakeNode(features, vid=vid, pid=pid)
    device = FakeDevice(replies if replies is not None else default_replies(),
                        grant=grant, stale=stale)
    kwargs.setdefault("settle", 0.0)
    kwargs.setdefault("grant_delay", 0.0)
    kwargs.setdefault("retries", 1)
    return s.MouseSession(node, device=device, **kwargs), device


def snapshot(obj) -> dict:
    """Value-only view of a settings object (survives a wire round-trip)."""
    as_dict = getattr(obj, "as_dict", None)
    if callable(as_dict):
        return as_dict()
    return {f.name: getattr(obj, f.name) for f in dataclasses.fields(obj)
            if not f.name.startswith("_")}


# ---------------------------------------------------------------- fixtures

@pytest.fixture
def features() -> dict:
    """A copy of the descriptor inventory the live dongle reports."""
    return dict(FEATURES)


@pytest.fixture
def build_frame():
    """``build(codec, key)`` -- the vendor-sized frame, before the trim."""
    return build


@pytest.fixture
def mouse_factory():
    """``make_mouse`` as a fixture, so tests do not import the helpers."""
    return make_mouse


@pytest.fixture
def mouse():
    """A session on a FakeDevice that answers every default report."""
    session, device = make_mouse()
    return session, device


@pytest.fixture
def event_report():
    """Build a five-byte interrupt-IN report the way the firmware sends it."""
    def build_event(code: int, param1: int = 0, param2: int = 0,
                    device_id: int = 0x01) -> bytes:
        return bytes((p.RID_EVENT, device_id, code, param1, param2))
    return build_event
