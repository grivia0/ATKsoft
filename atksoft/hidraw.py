"""Linux hidraw plumbing for ATKsoft.

Pure stdlib: the kernel's hidraw driver translates these ioctls into the USB
HID SET_REPORT / GET_REPORT control transfers the mouse firmware expects, so
neither hidapi nor libusb is required.

ioctl numbers come from <linux/hidraw.h>::

    #define HIDIOCGFEATURE(len) _IOC(_IOC_READ|_IOC_WRITE, 'H', 0x07, len)
    #define HIDIOCSFEATURE(len) _IOC(_IOC_READ|_IOC_WRITE, 'H', 0x06, len)

``len`` is the size of the whole transfer buffer *including* the report-ID
byte -- so for a feature report declared as 51 payload bytes in the report
descriptor you pass 52.

The node index (/dev/hidrawN) is NOT stable across replugs, so callers must
always locate the interface through :func:`enumerate_hidraw` and the report
descriptor rather than hard-coding a path.
"""

from __future__ import annotations

import array
import errno
import fcntl
import glob
import os
import select
from dataclasses import dataclass, field

__all__ = [
    "HIDIOCGFEATURE",
    "HIDIOCSFEATURE",
    "HidrawError",
    "HidrawNode",
    "HidrawDevice",
    "enumerate_hidraw",
    "parse_report_lengths",
    "feature_lengths",
]


# --------------------------------------------------------------------- ioctl

_IOC_READ = 2
_IOC_WRITE = 1


def _ioc(direction: int, typ: str, nr: int, size: int) -> int:
    return (direction << 30) | (size << 16) | (ord(typ) << 8) | nr


def HIDIOCGFEATURE(size: int) -> int:
    """ioctl number to read a feature report of ``size`` bytes (incl. report id)."""
    return _ioc(_IOC_READ | _IOC_WRITE, "H", 0x07, size)


def HIDIOCSFEATURE(size: int) -> int:
    """ioctl number to write a feature report of ``size`` bytes (incl. report id)."""
    return _ioc(_IOC_READ | _IOC_WRITE, "H", 0x06, size)


class HidrawError(OSError):
    """Any failure talking to a hidraw node."""


def describe_error(exc: OSError, path: str) -> str:
    if exc.errno in (errno.EACCES, errno.EPERM):
        return (
            f"no permission to open {path} -- install the udev rule "
            f"(atksoft install-udev) or run once with sudo"
        )
    if exc.errno == errno.EPIPE:
        return f"{path}: control endpoint stalled (EPIPE) -- the link is probably not live"
    if exc.errno == errno.EINVAL:
        return f"{path}: kernel rejected the report (EINVAL) -- length or report id mismatch"
    return f"{path}: {exc.strerror or exc}"


# --------------------------------------------------------- descriptor parsing

# Main-item prefixes (byte & 0xFC) we care about, plus the globals that feed
# them. Global items persist across collections exactly as the HID spec says.
_TAG_REPORT_ID = 0x84
_TAG_REPORT_SIZE = 0x74
_TAG_REPORT_COUNT = 0x94
_MAIN_ITEMS = {0x80: "input", 0x90: "output", 0xB0: "feature"}


def parse_report_lengths(descriptor: bytes) -> dict[int, dict[str, int]]:
    """Walk a HID report descriptor.

    Returns ``{report_id: {"input": n, "output": n, "feature": n}}`` where each
    ``n`` is the payload size in bytes (excluding the report-ID byte itself).
    """
    out: dict[int, dict[str, int]] = {}
    rid = 0
    size = 0
    count = 0
    i = 0
    end = len(descriptor)
    while i < end:
        b = descriptor[i]
        if b == 0xFE:  # long item: [0xFE, size, tag, ...data]
            i += 2 + descriptor[i + 1]
            continue
        tag = b & 0xFC
        blen = b & 0x03
        blen = 4 if blen == 3 else blen
        data = int.from_bytes(descriptor[i + 1:i + 1 + blen], "little") if blen else 0
        if tag == _TAG_REPORT_ID:
            rid = data
        elif tag == _TAG_REPORT_SIZE:
            size = data
        elif tag == _TAG_REPORT_COUNT:
            count = data
        elif tag in _MAIN_ITEMS:
            kind = _MAIN_ITEMS[tag]
            slot = out.setdefault(rid, {})
            slot[kind] = slot.get(kind, 0) + (size * count + 7) // 8
        i += 1 + blen
    return out


def feature_lengths(descriptor: bytes) -> dict[int, int]:
    """``{report_id: feature payload bytes}`` for reports that have any."""
    return {rid: kinds["feature"] for rid, kinds in parse_report_lengths(descriptor).items()
            if kinds.get("feature")}



# ---------------------------------------------------------------- enumeration

def _sysfs_interface(path: str) -> int | None:
    """USB bInterfaceNumber of the parent interface, if we can find it."""
    probe = os.path.realpath(path)
    for _ in range(6):
        probe = os.path.dirname(probe)
        if probe in ("", "/", "/sys"):
            return None
        candidate = os.path.join(probe, "bInterfaceNumber")
        if os.path.exists(candidate):
            try:
                with open(candidate) as fh:
                    return int(fh.read().strip(), 16)
            except (OSError, ValueError):
                return None
    return None


@dataclass
class HidrawNode:
    """One /dev/hidrawN node plus everything we can learn about it from sysfs."""

    path: str
    vid: int
    pid: int
    name: str = ""
    phys: str = ""
    interface: int | None = None
    descriptor: bytes = b""
    _lengths: dict[int, dict[str, int]] | None = field(default=None, repr=False)

    @property
    def report_lengths(self) -> dict[int, dict[str, int]]:
        if self._lengths is None:
            self._lengths = parse_report_lengths(self.descriptor)
        return self._lengths

    @property
    def features(self) -> dict[int, int]:
        return {rid: kinds["feature"] for rid, kinds in self.report_lengths.items()
                if kinds.get("feature")}

    def transfer_len(self, rid: int) -> int:
        """Total SET/GET_FEATURE buffer size for a feature report (id + payload)."""
        feat = self.features
        if rid not in feat:
            raise HidrawError(f"{self.path} has no feature report 0x{rid:02x}")
        return feat[rid] + 1

    def __str__(self) -> str:
        iface = "-" if self.interface is None else f"{self.interface}"
        return f"{self.path}  {self.vid:04x}:{self.pid:04x}  if{iface}  {self.name}"


def enumerate_hidraw() -> list[HidrawNode]:
    """Every hidraw node with a readable uevent + report descriptor.

    Sorted numerically so hidraw2 comes before hidraw10.
    """
    roots = sorted(glob.glob("/sys/class/hidraw/hidraw*"),
                   key=lambda p: int(p.rsplit("hidraw", 1)[1]))
    nodes: list[HidrawNode] = []
    for root in roots:
        try:
            with open(os.path.join(root, "device", "uevent")) as fh:
                props = dict(line.split("=", 1) for line in fh.read().splitlines() if "=" in line)
            with open(os.path.join(root, "device", "report_descriptor"), "rb") as fh:
                desc = fh.read()
        except OSError:
            continue
        parts = props.get("HID_ID", "").split(":")
        if len(parts) != 3:
            continue
        nodes.append(HidrawNode(
            path="/dev/" + os.path.basename(root),
            vid=int(parts[1], 16),
            pid=int(parts[2], 16),
            name=props.get("HID_NAME", ""),
            phys=props.get("HID_PHYS", ""),
            interface=_sysfs_interface(root),
            descriptor=desc,
        ))
    return nodes


# ------------------------------------------------------------------------- I/O

class HidrawDevice:
    """An open hidraw node: feature-report get/set + interrupt-IN reads."""

    def __init__(self, path: str):
        self.path = path
        self.fd: int | None = None
        #: Bytes the kernel moved in the last feature-transfer ioctl.  hidraw
        #: reports how much the firmware actually sent, which is the only way to
        #: notice that a read came back shorter than it was asked for (the
        #: buffer itself is always returned at the requested size).
        self.last_transfer: int | None = None

    # -- lifetime ---------------------------------------------------------

    def open(self) -> "HidrawDevice":
        if self.fd is not None:
            return self
        try:
            self.fd = os.open(self.path, os.O_RDWR | os.O_NONBLOCK)
        except OSError as exc:
            raise HidrawError(describe_error(exc, self.path)) from exc
        return self

    def close(self) -> None:
        if self.fd is not None:
            try:
                os.close(self.fd)
            finally:
                self.fd = None

    def __enter__(self) -> "HidrawDevice":
        return self.open()

    def __exit__(self, *exc_info) -> None:
        self.close()

    # -- feature reports --------------------------------------------------

    def get_feature(self, rid: int, transfer_len: int) -> bytearray:
        """Read a feature report. ``transfer_len`` includes the report-ID byte."""
        if self.fd is None:
            raise HidrawError("device is not open")
        buf = array.array("B", [0] * transfer_len)
        buf[0] = rid & 0xFF
        try:
            self.last_transfer = fcntl.ioctl(self.fd, HIDIOCGFEATURE(transfer_len), buf, True)
        except OSError as exc:
            self.last_transfer = None
            raise HidrawError(describe_error(exc, self.path)) from exc
        return bytearray(buf)

    def set_feature(self, payload) -> None:
        """Write a feature report. Length must match the descriptor exactly."""
        if self.fd is None:
            raise HidrawError("device is not open")
        buf = array.array("B", bytes(payload))
        try:
            fcntl.ioctl(self.fd, HIDIOCSFEATURE(len(buf)), buf, True)
        except OSError as exc:
            raise HidrawError(describe_error(exc, self.path)) from exc

    # -- interrupt IN -----------------------------------------------------

    def read(self, size: int = 64) -> bytes | None:
        """Non-blocking read; ``None`` when nothing is pending."""
        if self.fd is None:
            raise HidrawError("device is not open")
        try:
            return os.read(self.fd, size)
        except BlockingIOError:
            return None
        except OSError as exc:
            if exc.errno == errno.EAGAIN:
                return None
            raise HidrawError(describe_error(exc, self.path)) from exc

    def wait_readable(self, timeout: float) -> bool:
        if self.fd is None:
            raise HidrawError("device is not open")
        ready, _, _ = select.select([self.fd], [], [], timeout)
        return bool(ready)

