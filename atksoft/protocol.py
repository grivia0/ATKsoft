"""Wire protocol for the Attack Shark X6 (Beken 2.4G dongle, 1d57:fa60).

The X6's dongle ships byte-identical firmware to the Attack Shark X11's
(bcdDevice 11.08, four interfaces, report descriptors 67/73/230/27 bytes), so
every packet below is shared between the two mice.  All values here were
derived from the published X11 protocol documentation and cross-checked
against the X11 driver's own protocol builders.

Transport summary
-----------------
* One 230-byte interface carries everything: four interrupt-IN reports
  (ids 1/2/3) and 24 feature reports (ids 0x04..0x2e, 0xa0).
* Writes are HID SET_REPORT of an exact-length feature buffer.
* Reads need a permission round-trip first, because the firmware only hands
  out one report per grant:

      SET_FEATURE  a0 <rid> <declared_len> 00 01 00 00 00
      sleep 0.25s
      GET_FEATURE  a0                       -> byte[1] == 0x01 means granted
      GET_FEATURE  <rid>                    -> the payload (exactly once)

* Every command that mutates state is answered with an echo on the same
  feature report; reading it back verifies the write.
* Interrupt-IN report 0x03 carries unsolicited battery/status events.

Byte order
----------
Multi-byte sums are big-endian (the driver stores them with DataView's
default big-endian ``setInt16``).
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field

from .keynames import key_name, key_usage

# --------------------------------------------------------------- identifiers

VENDOR_ID = 0x1D57
PID_DONGLE = 0xFA60          # 2.4 GHz receiver (the config channel)
PID_WIRED = 0xFA55           # USB-C cable mode

# Feature report ids
RID_DPI = 0x04
RID_LIGHT = 0x05
RID_POLL = 0x06
RID_WAKE = 0x07
RID_BUTTONS = 0x08
RID_MACRO = 0x09
RID_PROFILE = 0x0A
RID_VERSION = 0x0B
RID_PROFILE_SET = 0x0C
RID_READ = 0xA0

# Interrupt-IN report id carrying battery/status events
RID_EVENT = 0x03

# byte[2] of the 0xA0 read-permission request.  The firmware compares it with
# the length it is prepared to emit and refuses the grant outright if the
# value is wrong, so these must be exact (note the odd 0x83 for macros:
# the request length is *not* the transfer length).
READ_GRANT_LENGTH = {
    RID_DPI: 0x38,
    RID_LIGHT: 0x0F,
    RID_POLL: 0x09,
    RID_BUTTONS: 0x3B,
    RID_MACRO: 0x83,
    RID_VERSION: 0x08,
    RID_PROFILE: 0x80,
    RID_PROFILE_SET: 0x0A,
}

# byte[1] of an outgoing write buffer (the vendor's packet-length field).
WRITE_HEADER_LENGTH = {
    RID_DPI: 0x38,
    RID_LIGHT: 0x0F,
    RID_POLL: 0x09,
    RID_WAKE: 0x09,
    RID_BUTTONS: 0x3B,
    RID_PROFILE_SET: 0x0A,
    RID_PROFILE: 0x80,
}

# Number of user-exposed DPI stages.  The vendor defaults the "active stages"
# bitmask to 0x3f (six stages) and its documentation lists six, although the
# report has room for eight.
DPI_STAGES = 6
MAX_DPI_STAGES = 8

DEBOUNCE_MS = (4, 6, 8, 10, 12, 14, 16, 20, 24, 28, 32, 36, 40, 44, 48, 52, 56, 60)



# ------------------------------------------------------------- poll rates

# byte[3] of the polling-rate report; byte[4] is its one's complement.
# Values above 1000 Hz are not exposed by the vendor tooling for this family.
POLL_BY_BYTE = {0x08: 125, 0x04: 250, 0x02: 500, 0x01: 1000}
BYTE_BY_POLL = {hz: b for b, hz in POLL_BY_BYTE.items()}


# --------------------------------------------------------------- lighting

# High nibble of byte[3]; the low nibble is reserved.  The two ``*-dpi`` modes
# belong to report 0x04: they light the LED in the *active DPI stage's* colour
# (``DPI_COLOR_OFFSET + 3 * stage``) instead of the single colour below, which is
# the only way the eight stage colours stay on the LED rather than just blinking
# past it.  Both of the vendor drivers for this family name them
# StaticDpi/BreathingDpi and put them at 0x05/0x06, where this table used to say
# "ripple" and "reactive" -- the second of which also collided with the
# unrelated ripple-control flag in report 0x04.
LIGHT_MODES = {
    "off": 0x00,             # vendor: Off
    "steady": 0x01,          # vendor: Static
    "breathing": 0x02,       # vendor: Breathing
    "neon": 0x03,            # vendor: Neon
    "rainbow": 0x04,         # vendor: ColorBreathing
    "steady-dpi": 0x05,      # vendor: StaticDpi, lit in the active stage's colour
    "breathing-dpi": 0x06,   # vendor: BreathingDpi, ditto
}
MODE_BY_VALUE = {v: k for k, v in LIGHT_MODES.items()}

#: The LED modes that colour the LED from the active DPI stage instead of
#: ``LightingSettings.rgb``.  In every other mode the LED keeps its own colour
#: and a DPI change only plays report 0x04's one-shot confirmation animation
#: (byte 49; see the ``DPI_INDICATOR_*`` values below), so editing a stage colour
#: there looks like it did nothing.
DPI_LIGHT_MODES = frozenset(("steady-dpi", "breathing-dpi"))

#: ``DPI_LIGHT_MODES`` as prose, for help text and tooltips.
DPI_LIGHT_MODES_TEXT = " or ".join(sorted(DPI_LIGHT_MODES))

BRIGHTNESS_MIN, BRIGHTNESS_MAX = 1, 8
LED_SPEED_MIN, LED_SPEED_MAX = 1, 5     # stored as (6 - speed) so 5 is fastest
SLEEP_MINUTES_MAX = 120
DEEP_SLEEP_MINUTES_MAX = 60


# --------------------------------------------------------------- battery

# params1 of a battery event.  The vendor's own enum calls these NORMAL,
# FULLY_CHARGED and CHARGING_IN_PROGRESS, while its prose documentation labels
# the first value "charging complete" -- which contradicts both the enum and
# the hardware, since a mouse running off its battery reports 1.
BATTERY_NORMAL = 0x01
BATTERY_FULLY_CHARGED = 0x02
BATTERY_CHARGING = 0x03
BATTERY_STATES = {
    BATTERY_NORMAL: "discharging",
    BATTERY_FULLY_CHARGED: "full",
    BATTERY_CHARGING: "charging",
}


# ----------------------------------------------------- button mapping table

# Firmware action byte -> human name.  Slot 5 (shipped as "forward") is
# read-only in the vendor tool; slot 6 is the DPI-cycle button.
FIRMWARE_ACTIONS = {
    0x01: "disabled",
    0x02: "left-click",
    0x03: "right-click",
    0x04: "middle-click",
    0x05: "backward",
    0x06: "forward",
    0x07: "double-click",
    0x08: "fire",
    0x09: "scroll-up",
    0x0A: "scroll-down",
    0x0D: "dpi-cycle",
    0x0E: "dpi-plus",
    0x0F: "dpi-minus",
    0x10: "easy-aim",
    0x11: "keyboard-key",
    0x12: "macro",
    0x15: "media-player",
    0x16: "previous-track",
    0x17: "next-track",
    0x18: "play-pause",
    0x19: "stop",
    0x1A: "mute",
    0x1B: "volume-up",
    0x1C: "volume-down",
    0x1D: "calculator",
    0x1E: "email",
    0x20: "browser-forward",
    0x21: "browser-backward",
    0x22: "browser-stop",
    0x23: "my-computer",
    0x24: "browser-refresh",
    0x25: "browser-home",
    0x26: "browser-search",
    0x34: "profile-cycle",
    0x35: "profile-up",
    0x36: "profile-down",
    0x3C: "mode-key",
    0x40: "polling-rate-cycle",
}
ACTION_BY_NAME = {name: code for code, name in FIRMWARE_ACTIONS.items()}

MODIFIERS = {"none": 0x00, "ctrl": 0x01, "shift": 0x02, "alt": 0x04, "win": 0x08}

# Slot -> byte offset inside the button report.
#
# One layout only: the response lists the slots in the order a write lists them,
# so a frame rebuilt from a read is byte-for-byte the frame the device sent and
# a read-modify-write cannot move a binding the user did not touch.  (An earlier
# revision of this table reversed slots 6/7/8 and 17/18, taken from the vendor's
# own reader, while ``build()`` used the write order: any write then swapped
# those four bindings on the device.  The live dongle's 0x08 frame is exactly
# what this layout rebuilds -- 04:dpi-cycle 05:dpi-plus 06:dpi-minus sitting at
# consecutive triples 12/15/18, 07:forward and 08:backward behind them.)  If a
# physical check ever shows the DPI button acting as the backward button, the
# firmware is reversing those slots and *both* tables have to be reversed
# together -- never one without the other.
BUTTON_SLOTS = 18
BUTTON_WRITE_OFFSET = {slot: 3 + 3 * (slot - 1) for slot in range(1, BUTTON_SLOTS + 1)}
BUTTON_READ_OFFSET = dict(BUTTON_WRITE_OFFSET)
BUTTON_NAMES = {
    1: "Left click", 2: "Right click", 3: "Middle click", 4: "Side button 1",
    5: "Side button 2 (read-only)", 6: "DPI cycle", 7: "Forward", 8: "Backward",
}
BUTTON_NAMES.update({slot: f"Slot {slot}" for slot in range(9, BUTTON_SLOTS + 1)})
READ_ONLY_SLOTS = frozenset({5})


# =====================================================================
# From here on the module stops being a table of constants and becomes
# the codec: one dataclass per feature report with ``build()`` (host ->
# wire) and ``from_bytes()`` (wire -> host), plus the read handshake and
# the interrupt-IN event decoder.
#
# What is deliberately *not* implemented:
#
#   * report 0x0a (RID_PROFILE) -- a 128-byte opaque profile blob; the
#     vendor driver has no builder or parser for it, so we only expose it
#     as a raw read (see ProfilePayload).
#   * report 0x09 (RID_MACRO) writes -- the vendor's writer and reader
#     disagree with each other (writer: checksum big-endian at packet 3
#     offset 10, actions from byte 30; reader: checksum little-endian at
#     absolute 129, actions from byte 29).  Reads are implemented because
#     the reading path is the one that demonstrably works; writes need
#     hardware confirmation first, see MACRO_WRITE_NOTES.
#   * report 0x07 (RID_WAKE) -- called WAKE_UP_MODE by the vendor, but
#     neither its tooling nor the published documentation describes the
#     payload, and a wrong value changes how the mouse sleeps, so see
#     WAKE_UP_MODE_NOTES.
# =====================================================================


# ----------------------------------------------------------------- errors


class ProtocolError(ValueError):
    """A report is malformed, out of range, or not understood."""


class ChecksumError(ProtocolError):
    """A report's trailing checksum does not match its payload."""


# ---------------------------------------------------------------- helpers


def _u16_be(buf, offset: int) -> int:
    return (buf[offset] << 8) | buf[offset + 1]


def _put_u16_be(buf, offset: int, value: int) -> None:
    value &= 0xFFFF
    buf[offset] = value >> 8
    buf[offset + 1] = value & 0xFF


def _u16_le(buf, offset: int) -> int:
    """Little-endian read; only the macro read response stores a sum this way."""
    return buf[offset] | (buf[offset + 1] << 8)


def checksum16(buf, start: int, end: int) -> int:
    """Plain 16-bit sum of ``buf[start:end]`` (the firmware's checksum)."""
    return sum(buf[start:end]) & 0xFFFF


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ProtocolError(message)


def _byte(value: int, what: str) -> int:
    _require(0 <= value <= 0xFF, f"{what} must fit in one byte (got {value!r})")
    return value & 0xFF


def _rgb(value) -> tuple[int, int, int]:
    try:
        r, g, b = value
    except (TypeError, ValueError) as exc:
        raise ProtocolError(f"colour must be three channels (got {value!r})") from exc
    return (_byte(r, "red"), _byte(g, "green"), _byte(b, "blue"))


# --------------------------------------------------------------- DPI codec

# PAW3311 sensitivity table.  A stage is stored as an X byte plus a Y byte:
#   Y = 0        -> index into this table, DPI = index * 50 + 50   (50 steps)
#   Y = 1        -> index into this table, DPI = index * 100 + 100 (100 steps)
#   Y >= 2       -> x | (y << 8) is an index past the table (10000+ DPI range)
# and a per-stage "double" flag multiplies the result by two, which is how
# values above 10000 DPI are reached.
DPI_3311 = (
    0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x08, 0x09, 0x0A, 0x0B, 0x0C, 0x0E, 0x0F, 0x10, 0x11, 0x12,
    0x13, 0x15, 0x16, 0x17, 0x18, 0x19, 0x1B, 0x1C, 0x1D, 0x1E, 0x1F, 0x20, 0x22, 0x23, 0x24, 0x25,
    0x26, 0x27, 0x29, 0x2A, 0x2B, 0x2C, 0x2D, 0x2F, 0x30, 0x31, 0x32, 0x33, 0x34, 0x36, 0x37, 0x38,
    0x39, 0x3A, 0x3B, 0x3D, 0x3E, 0x3F, 0x40, 0x41, 0x43, 0x44, 0x45, 0x46, 0x47, 0x48, 0x4A, 0x4B,
    0x4C, 0x4D, 0x4E, 0x4F, 0x51, 0x52, 0x53, 0x54, 0x55, 0x57, 0x58, 0x59, 0x5A, 0x5B, 0x5C, 0x5E,
    0x5F, 0x60, 0x61, 0x62, 0x63, 0x65, 0x66, 0x67, 0x68, 0x69, 0x6B, 0x6C, 0x6D, 0x6E, 0x6F, 0x70,
    0x72, 0x73, 0x74, 0x75, 0x76, 0x77, 0x79, 0x7A, 0x7B, 0x7C, 0x7D, 0x7F, 0x80, 0x81, 0x82, 0x83,
    0x84, 0x86, 0x87, 0x88, 0x89, 0x8A, 0x8B, 0x8D, 0x8E, 0x8F, 0x90, 0x91, 0x93, 0x94, 0x95, 0x96,
    0x97, 0x98, 0x9A, 0x9B, 0x9C, 0x9D, 0x9E, 0x9F, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5, 0xA7, 0xA8, 0xA9,
    0xAA, 0xAB, 0xAC, 0xAE, 0xAF, 0xB0, 0xB1, 0xB2, 0xB3, 0xB5, 0xB6, 0xB7, 0xB8, 0xB9, 0xBB, 0xBC,
    0xBD, 0xBE, 0xBF, 0xC0, 0xC2, 0xC3, 0xC4, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCC, 0xCD, 0xCF,
    0xD0, 0xD1, 0xD2, 0xD3, 0xD4, 0xD6, 0xD7, 0xD8, 0xD9, 0xDA, 0xDB, 0xDD, 0xDE, 0xDF, 0xE0, 0xE1,
    0xE3, 0xE4, 0xE5, 0xE6, 0xE7, 0xE8, 0xEA, 0xEB, 0x76, 0x77, 0x79, 0x7A, 0x7B, 0x7C, 0x7D, 0x7F,
    0x80, 0x81, 0x82, 0x83, 0x84, 0x86, 0x87, 0x88, 0x89, 0x8A, 0x8B, 0x8D,
)

DPI_MIN, DPI_MAX = 50, 26000
# The one value the firmware treats as a magic constant (0xeb / y=1 / double).
DPI_MAGIC = 20100
# Round-trippable DPI steps: 50 up to 10000, 100 up to 20000, then only 200
# (past 20000 the table tail repeats bytes, so odd hundreds read back wrong).
DPI_STEP_LIMITS = ((10000, 50), (20000, 100), (26000, 200))


def dpi_to_bytes(dpi: int) -> tuple[int, int, bool]:
    """Encode ``dpi`` for the PAW3311 as ``(x_byte, y_byte, is_double)``.

    ``0`` is a sentinel for "stage unused" and encodes to ``(0, 0, False)``.
    The vendor reference clamps it to 50 DPI instead, but then the all-zero
    stage 7/8 bytes of its own default report would not survive a
    decode/encode round-trip.
    """
    value = int(dpi)
    if value <= 0:
        return 0x00, 0x00, False
    value = max(DPI_MIN, min(value, DPI_MAX))
    if value == DPI_MAGIC:
        return 0xEB, 0x01, True
    is_double = False
    target = value
    if value > 10000:
        target = (value + 1) // 2          # the firmware's Math.round(v / 2)
        is_double = True
    if target > 22000:                     # present for completeness, see table note
        combined = 199 + (target - 10100) // 100
        return combined & 0xFF, (combined >> 8) & 0xFF, is_double
    if target > 5000 and target % 100 == 0:
        return DPI_3311[target // 100 - 1], 0x01, is_double
    return DPI_3311[(target - 50) // 50], 0x00, is_double


def bytes_to_dpi(x_byte: int, y_byte: int, is_double: bool) -> int:
    """Decode a stage's ``(x, y, double)`` triple back into DPI."""
    if x_byte == 0 and y_byte == 0:
        return 0
    if y_byte == 0:
        index = DPI_3311.index(x_byte) if x_byte in DPI_3311 else -1
        value = index * 50 + 50 if index >= 0 else DPI_MIN
    elif y_byte == 1:
        index = DPI_3311.index(x_byte) if x_byte in DPI_3311 else -1
        value = index * 100 + 100 if index >= 0 else 100
    else:
        value = 10100 + (((y_byte << 8) | x_byte) - 199) * 100
    if is_double:
        if x_byte == 0xEB:
            return DPI_MAGIC
        value *= 2
    return min(value, DPI_MAX)


# ----------------------------------------------------------- report lengths
#
# Every host-side buffer starts with the report-id byte, so these are total
# transfer lengths (descriptor payload + 1).  They are simultaneously the
# vendor's builder sizes and the lengths the 0xA0 grant request declares.
DPI_LENGTH = 0x38
LIGHT_LENGTH = 0x0F
POLL_LENGTH = 0x09
BUTTON_LENGTH = 0x3B
MACRO_LENGTH = 0x83
VERSION_LENGTH = 0x08
PROFILE_LENGTH = 0x80
PROFILE_SET_LENGTH = 0x0A
GRANT_LENGTH = 0x08
EVENT_LENGTH = 0x05

DPI_X_OFFSET = 8
DPI_Y_OFFSET = 16
DPI_DOUBLE_MASK_OFFSET = 6
DPI_TRIPLE_MASK_OFFSET = 7
DPI_STAGE_INDEX_OFFSET = 24
DPI_COLOR_OFFSET = 25
DPI_INDICATOR_OFFSET = 49
DPI_CHECKSUM_OFFSET = 50

# Byte 49 is the vendor's "DPI Active Indicator" -- its own comment on this byte,
# and the name of the ``dpiIndicationType`` field its reply parser reads.  It is
# not a stage number: it selects the *one-shot* animation the firmware plays when
# it accepts the report, and only three values do anything (the rest are
# ignored).  The animation borrows the LED for a moment and then hands it back to
# report 0x05, which owns the steady colour -- so a DPI write never moves the LED
# on its own, and ``session.MouseSession.apply`` re-sends report 0x05 after every
# DPI write for exactly that reason.
DPI_INDICATOR_NONE = 0      #: no animation: report 0x05's colour shows at once
DPI_INDICATOR_BLINK = 1     #: three quick blinks
DPI_INDICATOR_FLASH = 2     #: solid for roughly three seconds

#: Byte 49 value -> what the mouse does with it (the vendor's tool stores 2).
DPI_INDICATOR_EFFECTS = {
    DPI_INDICATOR_NONE: "no animation",
    DPI_INDICATOR_BLINK: "three quick blinks",
    DPI_INDICATOR_FLASH: "solid for about three seconds",
}


def dpi_indicator_effect(value: int) -> str:
    """What the mouse does with ``value`` in report 0x04's byte 49."""
    return DPI_INDICATOR_EFFECTS.get(int(value), "ignored by the firmware")

# Byte-for-byte copy of the vendor's own DPI default report (the constants its
# builder's constructor writes).  Its checksum 0x0f68 validates, its stages
# decode to [800, 1600, 2400, 3200, 5000, 22000, 0, 0], the colours match
# DPI_STAGE_COLORS below and byte 49 is 2 -- the vendor tool's own confirmation
# flash.  Used as a golden vector by the test suite.
DPI_VENDOR_DEFAULT = bytes((
    0x04, 0x38, 0x01, 0x00, 0x01, 0x3F, 0x20, 0x20,
    0x12, 0x25, 0x38, 0x4B, 0x75, 0x81, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x01, 0x00, 0x00,
    0x02,
    0xFF, 0x00, 0x00, 0x00, 0xFF, 0x00, 0x00, 0x00, 0xFF, 0xFF, 0xFF, 0x00,
    0x00, 0xFF, 0xFF, 0xFF, 0x00, 0xFF, 0xFF, 0x40, 0x00, 0xFF, 0xFF, 0xFF,
    0x02, 0x0F, 0x68, 0x00, 0x00, 0x00, 0x00,
))
DPI_STAGE_COLORS = (
    (0xFF, 0x00, 0x00), (0x00, 0xFF, 0x00), (0x00, 0x00, 0xFF), (0xFF, 0xFF, 0x00),
    (0x00, 0xFF, 0xFF), (0xFF, 0x00, 0xFF), (0xFF, 0x40, 0x00), (0xFF, 0xFF, 0xFF),
)
LIFT_OFF_DISTANCES = (0, 1, 2)


@dataclass
class DpiSettings:
    """Report 0x04: sensor DPI, stages, colours and sensor-level switches.

    ``_raw_stages`` keeps the ``(x, y, double)`` triples exactly as the device
    sent them, so a read-modify-write cycle cannot silently rewrite a stage
    that sits in the range where the firmware's own encoder and decoder
    disagree (anything past 20000 DPI; see :func:`bytes_to_dpi`).
    """

    profile: int = 1
    lift_off_distance: int = 0
    angle_snap: bool = False
    motion_sync: bool = False
    ripple_control: bool = True
    active_stages: int = 0x3F
    current_stage: int = 2
    dpi: list = field(default_factory=lambda: [800, 1600, 2400, 3200, 5000, 22000, 0, 0])
    colors: list = field(default_factory=lambda: list(DPI_STAGE_COLORS))
    #: Byte 49: the one-shot confirmation animation (``DPI_INDICATOR_*``), kept
    #: verbatim because the vendor's own tool stores 2 and never rewrites it.
    indicator: int = 2
    triple_mask: int = 0x20
    _raw_stages: list | None = field(default=None, repr=False, compare=False)

    @staticmethod
    def _stage_index(stage: int) -> int:
        _require(1 <= int(stage) <= MAX_DPI_STAGES, f"stage must be 1..{MAX_DPI_STAGES} (got {stage!r})")
        return int(stage) - 1

    def stage_dpi(self, stage: int) -> int:
        """DPI of ``stage`` (1-based)."""
        return self.dpi[self._stage_index(stage)]

    def set_stage_dpi(self, stage: int, value: int) -> None:
        index = self._stage_index(stage)
        self.dpi[index] = max(0, int(value))
        if self._raw_stages is not None:
            self._raw_stages[index] = dpi_to_bytes(self.dpi[index])

    def stage_enabled(self, stage: int) -> bool:
        return bool(self.active_stages & (1 << self._stage_index(stage)))

    def set_stage_enabled(self, stage: int, enabled: bool) -> None:
        bit = 1 << self._stage_index(stage)
        self.active_stages = (self.active_stages | bit) if enabled else (self.active_stages & ~bit)

    def enabled_stages(self) -> list:
        return [stage for stage in range(1, MAX_DPI_STAGES + 1) if self.stage_enabled(stage)]

    def stage_color(self, stage: int) -> tuple:
        return tuple(self.colors[self._stage_index(stage)])

    def set_stage_color(self, stage: int, rgb) -> None:
        self.colors[self._stage_index(stage)] = _rgb(rgb)

    # -- wire format ----------------------------------------------------
    def build(self) -> bytearray:
        """Serialise into a ``DPI_LENGTH``-byte write buffer."""
        _require(self.lift_off_distance in LIFT_OFF_DISTANCES,
                 f"lift-off distance must be one of {LIFT_OFF_DISTANCES}")
        buf = bytearray(DPI_LENGTH)
        buf[0] = RID_DPI
        buf[1] = DPI_LENGTH
        buf[2] = _byte(self.profile, "profile")
        buf[3] = ((self.lift_off_distance & 0x0F) << 4) | (0x01 if self.angle_snap else 0x00)
        buf[4] = (0x10 if self.motion_sync else 0x00) | (0x01 if self.ripple_control else 0x00)
        buf[5] = _byte(self.active_stages, "active stages")
        double_mask = 0
        for index in range(MAX_DPI_STAGES):
            if self._raw_stages is not None:
                x_byte, y_byte, is_double = self._raw_stages[index]
            else:
                x_byte, y_byte, is_double = dpi_to_bytes(self.dpi[index])
            buf[DPI_X_OFFSET + index] = x_byte
            buf[DPI_Y_OFFSET + index] = y_byte
            if is_double:
                double_mask |= 1 << index
        buf[DPI_DOUBLE_MASK_OFFSET] = double_mask
        buf[DPI_TRIPLE_MASK_OFFSET] = _byte(self.triple_mask, "Y double mask")
        buf[DPI_STAGE_INDEX_OFFSET] = _byte(self.current_stage, "current stage")
        for index in range(MAX_DPI_STAGES):
            base = DPI_COLOR_OFFSET + 3 * index
            buf[base:base + 3] = bytes(_rgb(self.colors[index]))
        buf[DPI_INDICATOR_OFFSET] = _byte(self.indicator, "DPI indicator")
        _put_u16_be(buf, DPI_CHECKSUM_OFFSET, checksum16(buf, 3, DPI_CHECKSUM_OFFSET))
        return buf

    @classmethod
    def from_bytes(cls, buf, verify: bool = True) -> "DpiSettings":
        """Parse a ``DPI_LENGTH``-byte read buffer (index 0 is the id byte)."""
        raw = bytes(buf)
        _require(len(raw) == DPI_LENGTH, f"DPI report must be {DPI_LENGTH} bytes, got {len(raw)}")
        if verify:
            expected = _u16_be(raw, DPI_CHECKSUM_OFFSET)
            actual = checksum16(raw, 3, DPI_CHECKSUM_OFFSET)
            if expected != actual:
                raise ChecksumError(f"DPI checksum {expected:#06x} != computed {actual:#06x}")
        dpi, colors, raw_stages = [], [], []
        for index in range(MAX_DPI_STAGES):
            x_byte = raw[DPI_X_OFFSET + index]
            y_byte = raw[DPI_Y_OFFSET + index]
            is_double = bool(raw[DPI_DOUBLE_MASK_OFFSET] >> index & 1)
            raw_stages.append((x_byte, y_byte, is_double))
            dpi.append(bytes_to_dpi(x_byte, y_byte, is_double))
            base = DPI_COLOR_OFFSET + 3 * index
            colors.append((raw[base], raw[base + 1], raw[base + 2]))
        lift = raw[3] >> 4
        return cls(
            profile=raw[2],
            lift_off_distance=lift if lift in LIFT_OFF_DISTANCES else 0,
            angle_snap=(raw[3] & 0x0F) == 0x01,
            motion_sync=(raw[4] >> 4) == 0x01,
            ripple_control=(raw[4] & 0x0F) == 0x01,
            active_stages=raw[5],
            current_stage=raw[DPI_STAGE_INDEX_OFFSET],
            dpi=dpi,
            colors=colors,
            indicator=raw[DPI_INDICATOR_OFFSET],
            triple_mask=raw[DPI_TRIPLE_MASK_OFFSET],
            _raw_stages=raw_stages,
        )

    def as_dict(self) -> dict:
        """Plain-data view for the CLI's ``--json`` output."""
        return {
            "profile": self.profile,
            "lift_off_distance": self.lift_off_distance,
            "angle_snap": self.angle_snap,
            "motion_sync": self.motion_sync,
            "ripple_control": self.ripple_control,
            "active_stages": self.active_stages,
            "current_stage": self.current_stage,
            "dpi": [{"stage": i + 1, "dpi": self.dpi[i]} for i in range(MAX_DPI_STAGES)],
            "colors": [{"stage": i + 1, "rgb": list(self.colors[i])} for i in range(MAX_DPI_STAGES)],
            "indicator": self.indicator,
        }


# ---------------------------------------------------------------- lighting

LIGHT_MODE_OFFSET = 3          # high nibble = mode, low nibble reserved
LIGHT_DEEP_LED_OFFSET = 4      # high nibble = deep sleep HIGH nibble, low = LED speed
LIGHT_DEEP_BRIGHT_OFFSET = 5   # high nibble = deep sleep LOW nibble, low = brightness
LIGHT_RGB_OFFSET = 6
LIGHT_SLEEP_OFFSET = 9         # sleep minutes * 2 (0.5 min steps, 1..60)
LIGHT_DEBOUNCE_OFFSET = 10     # debounce ms / 2
LIGHT_CHECKSUM_OFFSET = 11

# Byte-for-byte copy of the vendor's lighting default report.  Note that the
# deep-sleep nibbles are stored high-nibble-first: (byte4 & 0xF0) | (byte5 >> 4)
# decodes 0x0A = 10 minutes here.
LIGHT_VENDOR_DEFAULT = bytes((
    0x05, 0x0F, 0x01, 0x00, 0x03, 0xA8, 0x00, 0xFF, 0x00, 0x01, 0x04, 0x01, 0xAF, 0x00, 0x00,
))
SLEEP_CHOICES = tuple(step / 2 for step in range(1, 61))     # 0.5 .. 30.0 minutes
DEBOUNCE_CHOICES = tuple(range(4, 51, 2))                    # 4 .. 50 ms, step 2


def sleep_minutes(sleep_byte: int) -> float:
    """Byte 9 -> minutes.  Out-of-range bytes fall back to the smallest step."""
    if 1 <= sleep_byte <= len(SLEEP_CHOICES):
        return SLEEP_CHOICES[sleep_byte - 1]
    return SLEEP_CHOICES[0]


@dataclass
class LightingSettings:
    """Report 0x05: LED mode/colour, sleep timers and switch debounce."""

    profile: int = 1
    mode: int = LIGHT_MODES["off"]
    led_speed: int = 3
    brightness: int = 8
    deep_sleep: int = 10        # minutes before deep sleep, 1..60
    rgb: tuple = (0x00, 0xFF, 0x00)
    sleep: float = 0.5          # minutes before light-off, 0.5..30 in 0.5 steps
    debounce: int = 8           # ms, 4..50 in 2 ms steps

    @property
    def mode_name(self) -> str:
        return MODE_BY_VALUE.get(self.mode, f"unknown({self.mode:#04x})")

    @property
    def sleep_byte(self) -> int:
        """Byte 9: minutes * 2 (equivalently, the 1-based sleep-time LUT index)."""
        return int(round(self.sleep * 2))

    def build(self) -> bytearray:
        _require(self.mode in MODE_BY_VALUE, f"unknown light mode {self.mode!r}")
        _require(LED_SPEED_MIN <= self.led_speed <= LED_SPEED_MAX,
                 f"LED speed must be {LED_SPEED_MIN}..{LED_SPEED_MAX}")
        _require(BRIGHTNESS_MIN <= self.brightness <= BRIGHTNESS_MAX,
                 f"brightness must be {BRIGHTNESS_MIN}..{BRIGHTNESS_MAX}")
        _require(1 <= self.deep_sleep <= DEEP_SLEEP_MINUTES_MAX,
                 f"deep sleep must be 1..{DEEP_SLEEP_MINUTES_MAX} minutes")
        _require(self.sleep in SLEEP_CHOICES, f"sleep must be 0.5..30 minutes in 0.5 steps (got {self.sleep!r})")
        _require(self.debounce in DEBOUNCE_CHOICES,
                 f"debounce must be 4..50 ms in 2 ms steps (got {self.debounce!r})")
        red, green, blue = _rgb(self.rgb)
        buf = bytearray(LIGHT_LENGTH)
        buf[0] = RID_LIGHT
        buf[1] = LIGHT_LENGTH
        buf[2] = _byte(self.profile, "profile")
        buf[LIGHT_MODE_OFFSET] = (self.mode << 4) & 0xF0
        buf[LIGHT_DEEP_LED_OFFSET] = (self.deep_sleep & 0xF0) | (self.led_speed & 0x0F)
        buf[LIGHT_DEEP_BRIGHT_OFFSET] = ((self.deep_sleep & 0x0F) << 4) | (self.brightness & 0x0F)
        buf[LIGHT_RGB_OFFSET:LIGHT_RGB_OFFSET + 3] = bytes((red, green, blue))
        buf[LIGHT_SLEEP_OFFSET] = self.sleep_byte
        buf[LIGHT_DEBOUNCE_OFFSET] = self.debounce // 2
        _put_u16_be(buf, LIGHT_CHECKSUM_OFFSET, checksum16(buf, 3, LIGHT_CHECKSUM_OFFSET))
        return buf

    @classmethod
    def from_bytes(cls, buf, verify: bool = True) -> "LightingSettings":
        """Parse a ``LIGHT_LENGTH``-byte read buffer (index 0 is the id byte).

        The vendor's reader forgets to shift byte 3 back down (it hands the raw
        byte to a setter that shifts it *up* again, which corrupts the mode on
        a read/modify/write cycle).  We follow the writer's own convention,
        ``mode << 4`` on the wire / ``>> 4`` in memory.
        """
        raw = bytes(buf)
        _require(len(raw) == LIGHT_LENGTH, f"lighting report must be {LIGHT_LENGTH} bytes, got {len(raw)}")
        if verify:
            expected = _u16_be(raw, LIGHT_CHECKSUM_OFFSET)
            actual = checksum16(raw, 3, LIGHT_CHECKSUM_OFFSET)
            if expected != actual:
                raise ChecksumError(f"lighting checksum {expected:#06x} != computed {actual:#06x}")
        sleep_byte = raw[LIGHT_SLEEP_OFFSET]
        return cls(
            profile=raw[2],
            mode=raw[LIGHT_MODE_OFFSET] >> 4,
            led_speed=raw[LIGHT_DEEP_LED_OFFSET] & 0x0F,
            brightness=raw[LIGHT_DEEP_BRIGHT_OFFSET] & 0x0F,
            deep_sleep=(raw[LIGHT_DEEP_LED_OFFSET] & 0xF0) | (raw[LIGHT_DEEP_BRIGHT_OFFSET] >> 4),
            rgb=(raw[LIGHT_RGB_OFFSET], raw[LIGHT_RGB_OFFSET + 1], raw[LIGHT_RGB_OFFSET + 2]),
            sleep=sleep_minutes(sleep_byte),
            debounce=raw[LIGHT_DEBOUNCE_OFFSET] * 2,
        )

    def as_dict(self) -> dict:
        return {
            "profile": self.profile,
            "mode": self.mode,
            "mode_name": self.mode_name,
            "led_speed": self.led_speed,
            "brightness": self.brightness,
            "deep_sleep_minutes": self.deep_sleep,
            "rgb": list(self.rgb),
            "sleep_minutes": self.sleep,
            "debounce_ms": self.debounce,
        }


# ------------------------------------------------------------ polling rate

POLL_RATE_OFFSET = 3
POLL_INVERSE_OFFSET = 4
POLL_RATES = tuple(sorted(POLL_BY_BYTE.values(), reverse=True))     # 1000, 500, 250, 125


@dataclass
class PollingRate:
    """Report 0x06: the polling rate of one profile.

    Write-only in practice (the vendor re-sends a whole frame every time), but
    the firmware answers a read with the same layout.
    """

    profile: int = 1
    hz: int = 1000

    def build(self) -> bytearray:
        _require(self.hz in BYTE_BY_POLL, f"unsupported polling rate {self.hz!r} (use {POLL_RATES})")
        rate = BYTE_BY_POLL[self.hz]
        buf = bytearray(POLL_LENGTH)
        buf[0] = RID_POLL
        buf[1] = POLL_LENGTH
        buf[2] = _byte(self.profile, "profile")
        buf[POLL_RATE_OFFSET] = rate
        buf[POLL_INVERSE_OFFSET] = (0xFF - rate) & 0xFF
        return buf

    @classmethod
    def from_bytes(cls, buf) -> "PollingRate":
        """Parse a ``POLL_LENGTH``-byte read buffer.

        Byte 4 is byte 3's one's complement, but the vendor's reader ignores it
        and this firmware has been seen to answer with a stale frame, so we
        only require a rate byte we recognise and never reject the complement.
        """
        raw = bytes(buf)
        _require(len(raw) == POLL_LENGTH, f"polling report must be {POLL_LENGTH} bytes, got {len(raw)}")
        rate = raw[POLL_RATE_OFFSET]
        if rate not in POLL_BY_BYTE:
            raise ProtocolError(f"unsupported polling-rate byte {rate:#04x}")
        return cls(profile=raw[2], hz=POLL_BY_BYTE[rate])

    def as_dict(self) -> dict:
        return {"profile": self.profile, "hz": self.hz}


# -------------------------------------------------------- profile settings

PROFILE_CURRENT_OFFSET = 2
PROFILE_CURRENT_CHECK_OFFSET = 3
PROFILE_MAX_OFFSET = 4
PROFILE_MAX_CHECK_OFFSET = 5
PROFILE_SET_WIRED_LENGTH = 6        # wired mode truncates the report
MAX_PROFILES = 5                    # the vendor refuses to expose more


@dataclass
class ProfileSettings:
    """Report 0x0C: which profile is live and how many exist.

    Both fields are protected by their one's complement stored in the next
    byte, which is what the firmware validates.
    """

    current: int = 1
    maximum: int = 1

    def build(self, wired: bool = False) -> bytearray:
        _require(1 <= self.current <= MAX_PROFILES, f"current profile must be 1..{MAX_PROFILES}")
        _require(1 <= self.maximum <= MAX_PROFILES, f"profile count must be 1..{MAX_PROFILES}")
        buf = bytearray(PROFILE_SET_LENGTH)
        buf[0] = RID_PROFILE_SET
        buf[1] = PROFILE_SET_LENGTH
        buf[PROFILE_CURRENT_OFFSET] = self.current
        buf[PROFILE_CURRENT_CHECK_OFFSET] = (~self.current) & 0xFF
        buf[PROFILE_MAX_OFFSET] = self.maximum
        buf[PROFILE_MAX_CHECK_OFFSET] = (~self.maximum) & 0xFF
        return buf[:PROFILE_SET_WIRED_LENGTH] if wired else buf

    @classmethod
    def from_bytes(cls, buf, verify: bool = True) -> "ProfileSettings":
        raw = bytes(buf)
        _require(len(raw) in (PROFILE_SET_WIRED_LENGTH, PROFILE_SET_LENGTH),
                 f"profile-settings report must be {PROFILE_SET_WIRED_LENGTH} or "
                 f"{PROFILE_SET_LENGTH} bytes, got {len(raw)}")
        if verify:
            for value_offset, check_offset, what in (
                (PROFILE_CURRENT_OFFSET, PROFILE_CURRENT_CHECK_OFFSET, "current profile"),
                (PROFILE_MAX_OFFSET, PROFILE_MAX_CHECK_OFFSET, "profile count"),
            ):
                expected = (~raw[value_offset]) & 0xFF
                if expected != raw[check_offset]:
                    raise ChecksumError(
                        f"{what} checksum {raw[check_offset]:#04x} != computed {expected:#04x}")
        return cls(current=raw[PROFILE_CURRENT_OFFSET], maximum=raw[PROFILE_MAX_OFFSET])

    def as_dict(self) -> dict:
        return {"current": self.current, "maximum": self.maximum}


# =====================================================================
# Read permission (report 0xa0)
# =====================================================================
#
# The firmware hands out exactly one read per grant, so reading a report is a
# three-step dance:
#
#     SET_FEATURE  a0 <rid> <declared_len> 00 <parameter> 00 00 00
#     sleep 0.25 s
#     GET_FEATURE  a0                       -> byte[1] == 0x01 when granted
#     GET_FEATURE  <rid>                    -> the payload, exactly once
#
# ``declared_len`` must be what the firmware is prepared to emit, which is not
# always the descriptor's transfer size: a macro read answers with 0x83 bytes
# even though its write packets are 0x40.

GRANT_GRANTED = 0x01
GRANT_REFUSED = 0x00
GRANT_DELAY_S = 0.25

# What the parameter byte selects.  Reports that ignore it are still sent 0x01
# by the vendor driver, and the firmware accepts either value.
GRANT_PARAMETER = {
    RID_DPI: "profile id, 1..5",
    RID_BUTTONS: "profile id, 1..5",
    RID_MACRO: "macro id",
    RID_PROFILE_SET: "unused (the vendor sends 0x00)",
}
GRANT_DEFAULT_PARAMETER = 0x01

#: byte[4] of the request for reports whose vendor driver sends something else.
#: Report 0x0C is the only one: the vendor sends 0x00 there, and the firmware
#: *inserts* a non-zero parameter into the answer as payload byte[2], shifting
#: every field one byte to the right.  Read off the live dongle: ``raw 0x0c``
#: answers ``0c 0c 01 01 fe 01`` with the default 0x01 but ``0c 0c 01 fe 01 fe``
#: with 0x00 -- current=1, maximum=1 -- and only the latter matches the 0x0C
#: layout ``ProfileSettings`` reads (that pair of bytes is exactly what the
#: reader's checksums reject otherwise).
GRANT_PARAMETER_DEFAULT = {
    RID_PROFILE_SET: 0x00,
}


def build_grant_request(rid: int, parameter: int = GRANT_DEFAULT_PARAMETER,
                        declared_length: int | None = None) -> bytearray:
    """The 8-byte SET_FEATURE buffer that unlocks one read of ``rid``.

    ``declared_length`` overrides READ_GRANT_LENGTH; that is only useful when
    probing a report whose read length is unknown, where the descriptor's
    transfer length is the best guess available.
    """
    if declared_length is None:
        _require(rid in READ_GRANT_LENGTH,
                 f"report {rid:#04x} has no known read length; pass declared_length")
        declared_length = READ_GRANT_LENGTH[rid]
    buf = bytearray(GRANT_LENGTH)
    buf[0] = RID_READ
    buf[1] = _byte(rid, "report id")
    buf[2] = _byte(declared_length, "declared length")
    buf[3] = 0x00
    buf[4] = _byte(parameter, "parameter")
    return buf


def parse_grant_response(buf) -> bool:
    """True when an ``a0`` answer says the pending read was authorised."""
    raw = bytes(buf)
    _require(len(raw) >= 2, f"grant response must be at least 2 bytes, got {len(raw)}")
    return raw[1] == GRANT_GRANTED


def reply_length(frame) -> int | None:
    """The frame length the firmware *meant* to send, from byte[1] of a reply.

    Every answer to a granted read carries the declared read length + 2 in its
    second byte (report id + length byte + payload).  The live dongle confirms
    that for every report it exposes, which makes this byte a self-check on the
    grant -- and the only way to notice a truncated answer, because a reply can
    be shorter than the firmware intended: report 0x0C is 12 bytes inside the
    device but arrives as the 6 bytes the report descriptor declares.

    Returns ``None`` when the frame is too short to carry the field, or when
    byte[1] cannot be a length (the ``a0`` grant answer keeps a status there).

    :meth:`MouseSession.read_report` compares this against the length it asked
    for, because it is the one thing that separates a real answer from a buffer
    the dongle has not refilled yet.
    """
    raw = bytes(frame)
    if len(raw) < 2 or raw[1] < 2:
        return None
    return raw[1]


# =====================================================================
# Interrupt-IN events (report 0x03)
# =====================================================================
#
# Every interrupt-IN report is five bytes, always:
#
#     03 <device id> <event> <param1> <param2>
#
# The device id names the model.  Several Beken-based mice share this firmware
# (and therefore this event set); the X6 reports one of the ids below, but a
# decoder should pass the id through rather than insist on its own model.

DEVICE_IDS = {
    0x55: "X11",
    0x4D: "X3",
    0x4E: "X3 Max",
    0x85: "X6",
    0xBC: "M3",
}

MESSAGE_DPI_CYCLE = 0x10
MESSAGE_VIBRATION_MODE = 0x11
MESSAGE_BATTERY = 0x40
MESSAGE_BATTERY_ALT = 0x41
MESSAGE_COMMAND_CONFIRMATION = 0x50
MESSAGE_PROFILE_CHANGED = 0x80

MESSAGE_NAMES = {
    MESSAGE_DPI_CYCLE: "dpi-cycle",
    MESSAGE_VIBRATION_MODE: "vibration-mode",
    MESSAGE_BATTERY: "battery",
    MESSAGE_BATTERY_ALT: "battery-alt",
    MESSAGE_COMMAND_CONFIRMATION: "command-confirmation",
    MESSAGE_PROFILE_CHANGED: "profile-changed",
}

# param2 of a vibration-mode notification.
VIBRATION_MODES = {
    0x00: "disabled",
    0x01: "standard",
    0x02: "enhanced",
    0x03: "explosive",
}

# param1 of a command confirmation.
COMMAND_OK = 0x00
COMMAND_FAILED = 0x01


@dataclass(frozen=True)
class BatteryEvent:
    """Event 0x40/0x41: the device connection message, i.e. the battery.

    ``state`` is BATTERY_NORMAL (running off the battery),
    BATTERY_FULLY_CHARGED or BATTERY_CHARGING; per the vendor documentation the
    charging state also means the mouse is on its USB cable.  The first report
    after connecting often claims 100% before the ADC has measured anything,
    so keep the last report of a short window rather than the first.
    """

    state: int = BATTERY_NORMAL
    percent: int = 0

    @property
    def state_name(self) -> str:
        return BATTERY_STATES.get(self.state, f"unknown ({self.state})")

    @property
    def charging(self) -> bool:
        return self.state != BATTERY_NORMAL

    def as_dict(self) -> dict:
        return {
            "event": "battery",
            "state": self.state,
            "state_name": self.state_name,
            "percent": self.percent,
            "charging": self.charging,
        }


@dataclass(frozen=True)
class CommandConfirmationEvent:
    """Event 0x50: the firmware's answer to a feature-report write.

    ``report_id`` is the report that was written; ``ok`` reflects param1
    (COMMAND_OK or COMMAND_FAILED).  Not every write is confirmed, so a missing
    confirmation is not an error -- reading the value back is the real proof.
    """

    report_id: int = 0
    ok: bool = True

    @property
    def status(self) -> str:
        return "ok" if self.ok else "failed"

    def as_dict(self) -> dict:
        return {"event": "command-confirmation", "report_id": self.report_id,
                "ok": self.ok, "status": self.status}


@dataclass(frozen=True)
class ProfileChangedEvent:
    """Event 0x80: the profile the hardware button switched to (1-based)."""

    profile: int = 1

    def as_dict(self) -> dict:
        return {"event": "profile-changed", "profile": self.profile}


@dataclass(frozen=True)
class DpiCycleEvent:
    """Event 0x10: a DPI-cycle / DPI+ / DPI- press landed on this stage."""

    stage: int = 1

    def as_dict(self) -> dict:
        return {"event": "dpi-cycle", "stage": self.stage}


@dataclass(frozen=True)
class VibrationModeEvent:
    """Event 0x11: the vibration button selected a new mode (param2)."""

    mode: int = 0

    @property
    def mode_name(self) -> str:
        return VIBRATION_MODES.get(self.mode, f"unknown ({self.mode})")

    def as_dict(self) -> dict:
        return {"event": "vibration-mode", "mode": self.mode, "mode_name": self.mode_name}


@dataclass(frozen=True)
class UnknownEvent:
    """An event we cannot interpret (the firmware emits plenty of those)."""

    device_id: int = 0
    code: int = 0
    params: tuple = (0, 0)

    @property
    def device_name(self) -> str:
        return DEVICE_IDS.get(self.device_id, f"unknown ({self.device_id:#04x})")

    @property
    def code_name(self) -> str:
        return MESSAGE_NAMES.get(self.code, f"0x{self.code:02x}")

    def as_dict(self) -> dict:
        return {"event": self.code_name, "code": self.code, "device_id": self.device_id,
                "device_name": self.device_name, "params": list(self.params)}


Event = (BatteryEvent | CommandConfirmationEvent | ProfileChangedEvent
         | DpiCycleEvent | VibrationModeEvent | UnknownEvent)


def is_event_report(buf) -> bool:
    """True for a five-byte interrupt-IN report (as opposed to a stray read)."""
    raw = bytes(buf)
    return len(raw) == EVENT_LENGTH and raw[0] == RID_EVENT


def decode_event(buf) -> Event:
    """Decode one interrupt-IN report.

    Only the shape is validated (five bytes, report id 0x03); unknown event
    codes and parameters come back as :class:`UnknownEvent` instead of raising,
    because the firmware emits undocumented notifications all the time.
    """
    raw = bytes(buf)
    _require(len(raw) == EVENT_LENGTH,
             f"event report must be exactly {EVENT_LENGTH} bytes, got {len(raw)}")
    _require(raw[0] == RID_EVENT,
             f"not an event report (report id {raw[0]:#04x}, expected {RID_EVENT:#04x})")
    device_id, code, param1, param2 = raw[1], raw[2], raw[3], raw[4]
    if code in (MESSAGE_BATTERY, MESSAGE_BATTERY_ALT):
        return BatteryEvent(state=param1, percent=param2)
    if code == MESSAGE_COMMAND_CONFIRMATION:
        return CommandConfirmationEvent(report_id=param2, ok=param1 == COMMAND_OK)
    if code == MESSAGE_PROFILE_CHANGED:
        return ProfileChangedEvent(profile=param1 + 1)   # the device counts from 0
    if code == MESSAGE_DPI_CYCLE:
        return DpiCycleEvent(stage=param1)
    if code == MESSAGE_VIBRATION_MODE:
        return VibrationModeEvent(mode=param2)
    return UnknownEvent(device_id=device_id, code=code, params=(param1, param2))


# =====================================================================
# Report 0x08 -- button mapping
# =====================================================================
#
# 18 slots of three bytes each: <action> <modifiers> <usage>.  ``action`` is a
# FIRMWARE_ACTIONS value; ``modifiers`` is a MODIFIERS bitmask (keyboard keys
# only); ``usage`` is a HID usage id for "keyboard-key", a macro id for
# "macro", and zero otherwise.
#
# Two quirks are load-bearing:
#
#   * the read response does *not* list the slots in the write order.  Slots
#     6/7/8 come back as write-slots 8/7/6 and slots 17/18 as 18/17, which is
#     what BUTTON_READ_OFFSET encodes.
#   * the trailing checksum is placed differently in each direction.  The
#     vendor's builder writes a single byte at 58 and sums ``buf[3:59]`` -- the
#     byte it is about to overwrite is still zero, so that is the same sum as
#     ``buf[3:57]`` -- while the reader parses a big-endian u16 at 57.  Both
#     therefore carry the same number: the byte sum over ``buf[3:57]`` masked to
#     eight bits.  Reads accept either placement (and an unmasked 16-bit sum, in
#     case the firmware really keeps all 16 bits).

BUTTON_PROFILE_OFFSET = 2
BUTTON_DATA_OFFSET = 3
BUTTON_DATA_END = BUTTON_DATA_OFFSET + 3 * BUTTON_SLOTS      # 57
BUTTON_CHECKSUM_OFFSET_READ = BUTTON_DATA_END                # big-endian u16
BUTTON_CHECKSUM_OFFSET_WRITE = BUTTON_DATA_END + 1           # single byte

# The mapping the vendor's own builder starts from (``defaultSlots``): left,
# right and middle click, both side buttons and slots 9..16 disabled, the DPI
# button, forward/backward, then the two scroll directions on slots 17/18.
BUTTON_VENDOR_DEFAULT = (
    (ACTION_BY_NAME["left-click"], 0x00, 0x00),      # 1
    (ACTION_BY_NAME["right-click"], 0x00, 0x00),     # 2
    (ACTION_BY_NAME["middle-click"], 0x00, 0x00),    # 3
    (ACTION_BY_NAME["disabled"], 0x00, 0x00),        # 4
    (ACTION_BY_NAME["disabled"], 0x00, 0x00),        # 5
    (ACTION_BY_NAME["dpi-cycle"], 0x00, 0x00),       # 6
    (ACTION_BY_NAME["forward"], 0x00, 0x00),         # 7
    (ACTION_BY_NAME["backward"], 0x00, 0x00),        # 8
) + ((ACTION_BY_NAME["disabled"], 0x00, 0x00),) * 8 + (    # 9..16
    (ACTION_BY_NAME["scroll-up"], 0x00, 0x00),       # 17
    (ACTION_BY_NAME["scroll-down"], 0x00, 0x00),     # 18
)


def modifier_names(bits: int) -> list[str]:
    """Expand a modifier bitmask into names, e.g. ``0x03 -> ["ctrl", "shift"]``."""
    return [name for name, bit in MODIFIERS.items() if bit and bits & bit]


@dataclass
class ButtonSlot:
    """One button: what it does and, when needed, with what.

    ``usage`` carries the HID usage id for action "keyboard-key" and the macro
    id for action "macro"; it is zero for every other action.  ``modifiers`` is
    a MODIFIERS bitmask and is only meaningful for keyboard keys.
    """

    action: int = ACTION_BY_NAME["disabled"]
    modifiers: int = 0x00
    usage: int = 0x00

    @property
    def action_name(self) -> str:
        return FIRMWARE_ACTIONS.get(self.action, f"unknown ({self.action:#04x})")

    def describe(self) -> str:
        """One-line human description of the binding."""
        if self.action == ACTION_BY_NAME["keyboard-key"]:
            parts = modifier_names(self.modifiers) + [key_name(self.usage)]
            return " + ".join(parts)
        if self.action == ACTION_BY_NAME["macro"]:
            return f"macro #{self.usage}"
        if self.modifiers:
            return f"{self.action_name} ({'+'.join(modifier_names(self.modifiers))})"
        return self.action_name

    def as_dict(self) -> dict:
        return {
            "action": self.action,
            "action_name": self.action_name,
            "modifiers": self.modifiers,
            "modifier_names": modifier_names(self.modifiers),
            "usage": self.usage,
            "description": self.describe(),
        }


def parse_action_spec(spec: str) -> ButtonSlot:
    """Turn ``"double-click"``, ``"key:shift+a"`` or ``"macro:3"`` into a slot.

    Accepted forms::

        <firmware-action>              disabled, left-click, fire, dpi-cycle, ...
        key:<name|usage>[+<modifiers>]  key:a, key:Left Ctrl, key:0x04, key:shift+a
        macro:<id>                      the macro the button should run

    Modifier names come from MODIFIERS (ctrl, shift, alt, win) and may sit on
    either side of the key, so ``key:shift+a`` and ``key:a+shift`` are the same
    binding -- and ``ctrl+key:a`` works too.  The ``usage`` may be a display name
    from :mod:`atksoft.keynames` (case-insensitive) or a number in decimal or
    hex; keys whose display name contains ``+`` must be given numerically, e.g.
    ``key:0x57`` for the keypad plus.  A lone ``key:<modifier>`` has no key in
    it, so the Shift key itself is ``key:0xe1``.
    """
    tokens = [token.strip() for token in str(spec).split("+")]
    _require(all(tokens), f"action spec {spec!r} has an empty part")

    modifiers = 0
    keys = []                       # (kind or None, token) pairs that are not modifiers
    kinds = []                      # colon-prefixes seen on modifier tokens
    for token in tokens:
        kind, colon, argument = token.lower().partition(":")
        modifier = MODIFIERS.get(argument if colon else kind, 0x00)
        if modifier:
            # a bare modifier ("ctrl") or one tucked inside the key
            # ("key:shift+a", which the vendor's own format uses)
            modifiers |= modifier
            if colon:
                kinds.append(kind)
        else:
            keys.append((kind, argument) if colon else (None, token))
    _require(len(keys) == 1,
             f"action spec {spec!r} needs exactly one key or action ({len(keys)} given)")
    kind, token = keys[0]
    _require(not kinds or kind is None or kinds == [kind],
             f"action spec {spec!r} mixes action kinds "
             f"{sorted({name for name in (kind, *kinds) if name})}")
    if kind is None and kinds:
        kind = kinds[0]             # "key:ctrl+a": the "key:" prefix carries over

    if kind == "key":
        action = ACTION_BY_NAME["keyboard-key"]
        try:
            usage = key_usage(token)
        except ValueError as error:         # keynames raises plain ValueError
            raise ProtocolError(f"{error} (in {spec!r})") from None
    elif kind == "macro":
        action = ACTION_BY_NAME["macro"]
        try:
            usage = _byte(int(token, 0), "macro id")
        except ValueError:
            raise ProtocolError(f"macro id {token!r} in {spec!r} is not a number") from None
    elif kind is None:
        name = token.lower()
        _require(name in ACTION_BY_NAME, f"unknown action {name!r} in {spec!r}")
        _require(not modifiers, f"modifiers only apply to keys, use 'key:...' (got {spec!r})")
        action, usage = ACTION_BY_NAME[name], 0x00
    else:
        raise ProtocolError(f"unknown action kind {kind!r} in {spec!r} (have 'key', 'macro')")
    return ButtonSlot(action=action, modifiers=modifiers, usage=usage)


def format_action_spec(slot: ButtonSlot) -> str:
    """Best-effort inverse of :func:`parse_action_spec`.

    Keys with an awkward display name (the keypad set, which contains spaces
    and punctuation) fall back to their numeric form so the result always
    parses again.
    """
    if slot.action == ACTION_BY_NAME["macro"]:
        return f"macro:{slot.usage}"
    if slot.action == ACTION_BY_NAME["keyboard-key"]:
        name = key_name(slot.usage)
        token = name if name.replace(" ", "").isalnum() else f"0x{slot.usage:02x}"
        spec = f"key:{token}"
        if slot.modifiers:
            spec += "+" + "+".join(modifier_names(slot.modifiers))
        return spec
    return slot.action_name


@dataclass
class ButtonSettings:
    """Report 0x08: the button map of one profile.

    ``slots[0]`` is slot 1 (left click) and ``slots[17]`` is slot 18, matching
    the numbering in BUTTON_NAMES.
    """

    profile: int = 1
    slots: list = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.slots:
            self.slots = [ButtonSlot() for _ in range(BUTTON_SLOTS)]
        _require(len(self.slots) == BUTTON_SLOTS,
                 f"a button report holds exactly {BUTTON_SLOTS} slots, got {len(self.slots)}")

    @classmethod
    def default(cls, profile: int = 1) -> "ButtonSettings":
        """The factory mapping (as shipped by the vendor's own default frame)."""
        return cls(profile=profile, slots=[ButtonSlot(*triple) for triple in BUTTON_VENDOR_DEFAULT])

    def slot(self, number: int) -> ButtonSlot:
        _require(1 <= number <= BUTTON_SLOTS,
                 f"button slot must be 1..{BUTTON_SLOTS} (got {number!r})")
        return self.slots[number - 1]

    def set_slot(self, number: int, action, modifiers: int = 0x00, usage: int = 0x00,
                 allow_read_only: bool = False) -> ButtonSlot:
        """Replace one slot; ``action`` may be a spec string or an action byte.

        Slot 5 (the second side button) is read-only in the vendor tool, so it
        is refused unless ``allow_read_only`` is set -- the firmware itself may
        well accept the write.
        """
        if isinstance(action, str):
            _require(not (modifiers or usage),
                     "pass either an action spec or explicit action/modifiers/usage")
            new = parse_action_spec(action)
        else:
            new = ButtonSlot(_byte(action, "action"), _byte(modifiers, "modifiers"),
                             _byte(usage, "usage"))
        _require(allow_read_only or number not in READ_ONLY_SLOTS,
                 f"slot {number} ({BUTTON_NAMES.get(number, '')}) is read-only; "
                 f"pass allow_read_only=True to try anyway")
        self.slots[number - 1] = new
        return new

    def as_dict(self) -> dict:
        slots = []
        for number, slot in enumerate(self.slots, start=1):
            entry = {"number": number, "name": BUTTON_NAMES.get(number, f"Slot {number}")}
            entry.update(slot.as_dict())
            slots.append(entry)
        return {"profile": self.profile, "slots": slots}

    # -- wire format ----------------------------------------------------
    def build(self) -> bytearray:
        """Serialise into a ``BUTTON_LENGTH``-byte write buffer (id 0x08)."""
        _require(1 <= self.profile <= MAX_PROFILES, f"profile must be 1..{MAX_PROFILES}")
        buf = bytearray(BUTTON_LENGTH)
        buf[0] = RID_BUTTONS
        buf[1] = BUTTON_LENGTH
        buf[BUTTON_PROFILE_OFFSET] = _byte(self.profile, "profile")
        for number, slot in enumerate(self.slots, start=1):
            offset = BUTTON_WRITE_OFFSET[number]
            buf[offset] = _byte(slot.action, "action")
            buf[offset + 1] = _byte(slot.modifiers, "modifiers")
            buf[offset + 2] = _byte(slot.usage, "usage")
        buf[BUTTON_CHECKSUM_OFFSET_WRITE] = \
            checksum16(buf, BUTTON_DATA_OFFSET, BUTTON_DATA_END) & 0xFF
        return buf

    @classmethod
    def from_bytes(cls, buf, verify: bool = True) -> "ButtonSettings":
        """Parse a ``BUTTON_LENGTH``-byte read buffer (index 0 is the id byte).

        The slot order of the response is *not* the write order -- see
        BUTTON_READ_OFFSET.  Both the single-byte and the 16-bit placement of
        the trailing sum are accepted (see the notes above the constants).
        """
        raw = bytes(buf)
        _require(len(raw) == BUTTON_LENGTH,
                 f"button report must be {BUTTON_LENGTH} bytes, got {len(raw)}")
        if verify:
            actual = checksum16(raw, BUTTON_DATA_OFFSET, BUTTON_DATA_END)
            found = _u16_be(raw, BUTTON_CHECKSUM_OFFSET_READ)
            if found not in (actual, actual & 0xFF):
                raise ChecksumError(
                    f"button checksum {found:#06x} matches neither {actual:#06x} "
                    f"nor {actual & 0xFF:#04x}")
        return cls(
            profile=raw[BUTTON_PROFILE_OFFSET],
            slots=[ButtonSlot(raw[BUTTON_READ_OFFSET[number]],
                              raw[BUTTON_READ_OFFSET[number] + 1],
                              raw[BUTTON_READ_OFFSET[number] + 2])
                   for number in range(1, BUTTON_SLOTS + 1)],
        )


# =====================================================================
# Report 0x09 -- custom macros
# =====================================================================
#
# A macro is written as three 64-byte feature packets (pages 0/1/2) and read
# back as a single 131-byte report.  The two directions do not agree on every
# detail -- see MACRO_WRITE_NOTES -- so the writer mirrors the vendor's own
# builder byte for byte rather than trying to be clever about it.
#
# Every action is two bytes -- ``<direction|delay> <key>`` -- or four when the
# delay needs more than seven bits of 10 ms units (``... <200ms blocks> 0x03``).
# An extended action bumps the action *count* field by two, so that field is not
# the number of actions.

MACRO_TYPE_OFFSET = 3
MACRO_GUN_RGB_OFFSET = 4
MACRO_LOOP_OFFSET = 7
MACRO_NAME_OFFSET = 8
MACRO_NAME_LENGTH = 20
MACRO_COUNT_OFFSET = 28
MACRO_ACTIONS_OFFSET = 29
MACRO_ACTION_BYTES = 100                 # size of the action pool, in bytes
MACRO_PAGE_LENGTH = 0x40                 # each of the three write packets
MACRO_READ_CHECKSUM_OFFSET = 129         # read: little-endian u16
MACRO_WRITE_CHECKSUM_OFFSET = 10         # page 2: big-endian int16
MACRO_EXTENDED_FLAG = 0x03
MACRO_DELAY_UNIT_MS = 10
MACRO_DELAY_BLOCK_MS = 200
MACRO_DELAY_MS_MAX = 50000
MACRO_DELAY_BLOCKS_MAX = 0xFF
MACRO_DIRECTIONS = {0x00: "press", 0x80: "release"}
MACRO_TYPES = {0x00: "fixed-loop", 0x01: "until-key-press", 0x02: "while-pressed"}
MACRO_TYPE_BY_NAME = {name: value for value, name in MACRO_TYPES.items()}

MACRO_WRITE_NOTES = """\
The two directions of report 0x09 disagree on the trailing checksum, so treat
writes as experimental until hardware confirms a round-trip:

* byte order -- the writer puts a big-endian int16 at page 2 offsets 10-11, the
  reader expects a little-endian uint16 at offsets 129-130.  The two agree only
  when the sum fits in the low byte;
* coverage -- the writer sums page 0[4:64], page 1[4:64] and page 2[4:9] (125
  bytes), deliberately skipping page 2 offset 9, which holds action byte 99;
  the reader sums offsets 3..128 (126 bytes, that byte included).  A macro we
  write therefore reads back with a checksum that is off by the value of its
  last action byte;
* the writer feeds the sum to setInt16, which throws above 0x7fff.

Reading is unaffected, so the reader below is the authoritative decode; a frame
written by us has to be read back with verify=False until this is settled.
"""


@dataclass
class MacroAction:
    """One press or release inside a macro.

    ``button`` is a HID keyboard usage id, or one of the macro mouse codes
    0xF1..0xF5 (see :mod:`atksoft.keynames`).  ``delay_ms`` is the pause before
    the event, in 10 ms steps; above 1270 ms the encoding grows a byte.
    """

    button: int = 0x04                      # "a", the vendor's own default
    direction: int = 0x00                   # MACRO_DIRECTIONS
    delay_ms: int = 10

    def __post_init__(self) -> None:
        self.button = _byte(self.button, "button")
        self.direction = _byte(self.direction, "direction")
        _require(self.direction in MACRO_DIRECTIONS,
                 f"direction must be one of {sorted(MACRO_DIRECTIONS)} (got {self.direction:#04x})")
        _require(0 <= self.delay_ms <= MACRO_DELAY_MS_MAX,
                 f"delay must be 0..{MACRO_DELAY_MS_MAX} ms (got {self.delay_ms!r})")
        _require(self.delay_ms % MACRO_DELAY_UNIT_MS == 0,
                 f"delay must be a multiple of {MACRO_DELAY_UNIT_MS} ms (got {self.delay_ms!r})")

    @property
    def delay_units(self) -> int:
        return self.delay_ms // MACRO_DELAY_UNIT_MS

    @property
    def is_extended(self) -> bool:
        """True when the delay needs the four-byte encoding."""
        return self.delay_units > 0x7F

    @property
    def wire_length(self) -> int:
        return 4 if self.is_extended else 2

    @property
    def direction_name(self) -> str:
        return MACRO_DIRECTIONS[self.direction]

    def to_bytes(self) -> bytes:
        """Encode as two bytes, or four when the delay is long."""
        if not self.is_extended:
            return bytes((self.direction | self.delay_units, self.button))
        blocks, remainder = divmod(self.delay_ms, MACRO_DELAY_BLOCK_MS)
        _require(blocks <= MACRO_DELAY_BLOCKS_MAX,
                 f"delay {self.delay_ms} ms needs {blocks} blocks "
                 f"(max {MACRO_DELAY_BLOCKS_MAX})")
        return bytes((self.direction | (remainder // MACRO_DELAY_UNIT_MS),
                      self.button, blocks, MACRO_EXTENDED_FLAG))

    def describe(self) -> str:
        return f"{self.direction_name} {key_name(self.button)} after {self.delay_ms} ms"

    def as_dict(self) -> dict:
        return {
            "button": self.button,
            "button_name": key_name(self.button),
            "direction": self.direction,
            "direction_name": self.direction_name,
            "delay_ms": self.delay_ms,
            "extended": self.is_extended,
        }


def _fixed_text(raw) -> str:
    """Decode a NUL-padded fixed-width UTF-8 field."""
    return bytes(raw).split(b"\x00", 1)[0].decode("utf-8", errors="replace")


def _fixed_utf8(text: str, length: int) -> bytes:
    """Encode ``text`` into exactly ``length`` bytes, cutting on a character edge."""
    encoded = str(text).encode("utf-8")[:length]
    while encoded:
        try:
            encoded.decode("utf-8")
            break
        except UnicodeDecodeError:
            encoded = encoded[:-1]
    return encoded.ljust(length, b"\x00")


def parse_macro_actions(pool) -> list:
    """Decode the 100-byte action pool of a macro.

    The pool is a flat byte string rather than an array of fixed records: an
    action is two bytes, or four when its delay exceeds 1270 ms, and a pair of
    zero bytes ends the list.  As in the vendor's reader, the extended flag is
    looked for in the *third* byte of the following record, so a macro whose
    next action happens to use usage 0x03 can be misread -- hardware behaviour
    is the reference here.
    """
    raw = bytes(pool)
    actions = []
    index = 0
    while index + 1 < len(raw):
        delay_byte, button = raw[index], raw[index + 1]
        if not delay_byte and not button:
            break
        direction = 0x80 if delay_byte & 0x80 else 0x00
        delay_ms = (delay_byte & 0x7F) * MACRO_DELAY_UNIT_MS
        index += 2
        if index + 1 < len(raw) and raw[index + 1] == MACRO_EXTENDED_FLAG:
            delay_ms += raw[index] * MACRO_DELAY_BLOCK_MS
            index += 2
        actions.append(MacroAction(button=button, direction=direction, delay_ms=delay_ms))
    return actions


@dataclass
class Macro:
    """One macro slot: playback mode, loop count, name, gun colour and actions.

    ``macro_type`` is deliberately not checked against MACRO_TYPES here, so an
    unknown value read off the device can be displayed instead of rejected; the
    writer is stricter than the reader.
    """

    id: int = 1
    macro_type: int = 0x00
    gun_rgb: tuple = (0x00, 0x00, 0x00)
    loop_times: int = 1
    name: str = "macro1"
    actions: list = field(default_factory=list)

    def __post_init__(self) -> None:
        self.id = _byte(self.id, "macro id")
        self.macro_type = _byte(self.macro_type, "macro type")
        self.gun_rgb = _rgb(self.gun_rgb)
        self.loop_times = _byte(self.loop_times, "loop times")
        self.actions = [action if isinstance(action, MacroAction) else MacroAction(*action)
                        for action in self.actions]

    @property
    def type_name(self) -> str:
        return MACRO_TYPES.get(self.macro_type, f"unknown ({self.macro_type:#04x})")

    @property
    def action_count(self) -> int:
        """The value of the action-count field: two per extended action."""
        return sum(2 if action.is_extended else 1 for action in self.actions)

    def action_pool(self) -> bytes:
        """Serialise the actions into the flat pool the report carries."""
        pool = b"".join(action.to_bytes() for action in self.actions)
        _require(len(pool) <= MACRO_ACTION_BYTES,
                 f"the action pool holds {MACRO_ACTION_BYTES} bytes; {len(pool)} given")
        return pool

    def describe(self) -> str:
        return (f"macro #{self.id} {self.name!r} ({self.type_name}, "
                f"{self.loop_times} loop(s), {len(self.actions)} action(s))")

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "type": self.macro_type,
            "type_name": self.type_name,
            "loop_times": self.loop_times,
            "gun_rgb": list(self.gun_rgb),
            "action_count": self.action_count,
            "actions": [action.as_dict() for action in self.actions],
        }

    @classmethod
    def from_bytes(cls, buf, verify: bool = True) -> "Macro":
        """Parse a ``MACRO_LENGTH``-byte read buffer (index 0 is the id byte).

        The trailing checksum is accepted in either byte order, because the
        writer and the reader disagree about it -- see MACRO_WRITE_NOTES.
        """
        raw = bytes(buf)
        _require(len(raw) == MACRO_LENGTH,
                 f"macro report must be {MACRO_LENGTH} bytes, got {len(raw)}")
        if verify:
            actual = checksum16(raw, 3, MACRO_READ_CHECKSUM_OFFSET)
            found = (_u16_le(raw, MACRO_READ_CHECKSUM_OFFSET),
                     _u16_be(raw, MACRO_READ_CHECKSUM_OFFSET))
            if actual not in found:
                raise ChecksumError(f"macro checksum {found[0]:#06x}/{found[1]:#06x} "
                                    f"(little/big endian) != computed {actual:#06x}")
        return cls(
            id=raw[2],
            macro_type=raw[MACRO_TYPE_OFFSET],
            gun_rgb=(raw[MACRO_GUN_RGB_OFFSET], raw[MACRO_GUN_RGB_OFFSET + 1],
                     raw[MACRO_GUN_RGB_OFFSET + 2]),
            loop_times=raw[MACRO_LOOP_OFFSET],
            name=_fixed_text(raw[MACRO_NAME_OFFSET:MACRO_NAME_OFFSET + MACRO_NAME_LENGTH]),
            actions=parse_macro_actions(
                raw[MACRO_ACTIONS_OFFSET:MACRO_ACTIONS_OFFSET + MACRO_ACTION_BYTES]),
        )


def build_macro_pages(macro: Macro) -> list:
    """Build the three 64-byte packets that write ``macro``.

    Page 0, page 1 and page 2 are returned in the order the device expects, each
    to be sent as its own ``SET_REPORT`` of report id 0x09.  See
    MACRO_WRITE_NOTES for the checksum caveat that makes writing experimental.
    """
    _require(macro.macro_type in MACRO_TYPES,
             f"macro type must be one of {sorted(MACRO_TYPES)} (got {macro.macro_type:#04x})")
    pool = macro.action_pool()
    pages = []
    for index in range(3):
        page = bytearray(MACRO_PAGE_LENGTH)
        page[0] = RID_MACRO
        page[1] = MACRO_PAGE_LENGTH
        page[2] = macro.id
        page[3] = index
        pages.append(page)
    first = pages[0]
    first[MACRO_TYPE_OFFSET] = macro.macro_type
    first[MACRO_GUN_RGB_OFFSET:MACRO_GUN_RGB_OFFSET + 3] = bytes(macro.gun_rgb)
    first[MACRO_LOOP_OFFSET] = macro.loop_times
    first[MACRO_NAME_OFFSET:MACRO_NAME_OFFSET + MACRO_NAME_LENGTH] = \
        _fixed_utf8(macro.name, MACRO_NAME_LENGTH)
    first[MACRO_COUNT_OFFSET] = _byte(macro.action_count, "action count")
    first[30:64] = pool[:34].ljust(34, b"\x00")
    pages[1][4:64] = pool[34:94].ljust(60, b"\x00")
    pages[2][4:10] = pool[94:100].ljust(6, b"\x00")
    checksum = (sum(first[4:]) + sum(pages[1][4:]) + sum(pages[2][4:9])) & 0xFFFF
    _put_u16_be(pages[2], MACRO_WRITE_CHECKSUM_OFFSET, checksum)
    return pages


# =====================================================================
# Reports the vendor's library never decodes: 0x07, 0x0A, 0x0B
# =====================================================================
#
# These three feature reports exist in the descriptor but have no builder and no
# response handler upstream, so only their framing is known.  They are exposed
# as raw frames: the CLI can dump one, and a frame read off the device can be
# echoed back unchanged.  A single live read settles what no reference
# implementation documents.

WAKE_LENGTH = 0x09                      # from WRITE_HEADER_LENGTH[RID_WAKE]

VERSION_NOTES = """\
Report 0x0B is declared by the vendor's library and then forgotten: types.ts
carries only the id (0x0b) and the read length (0x08) -- there is no version
builder and no response handler.  Eight bytes of unknown layout, kept verbatim.
"""

PROFILE_BLOB_NOTES = """\
Report 0x0A is an opaque 128-byte blob.  The vendor only ever reads it (0x0C is
the writable profile report, implemented above as ProfileSettings), and it is
most likely the same profile record the GUI round-trips after editing, so it is
kept byte-exact rather than interpreted.
"""

WAKE_UP_MODE_NOTES = """\
Report 0x07 has no implementation anywhere upstream: the descriptor gives it a
nine-byte feature report, WRITE_HEADER_LENGTH lists 0x09, and it is missing from
READ_GRANT_LENGTH -- so even the declared length of a read request is a guess.
The likely subject is the vibration ("wake up") mode, which the firmware also
notifies about unsolicited on interrupt report 0x03 with message code 0x11, so
that notification is the safe way to observe the current setting; reading and
writing the report itself still needs a hardware experiment.
"""


@dataclass
class RawReport:
    """A feature report kept verbatim: header understood, payload opaque.

    ``report_id`` and ``length`` are the first two bytes of the transfer and the
    payload is whatever follows.  :meth:`build` re-emits the frame unchanged,
    which makes it safe to read a report and write it straight back.
    """

    report_id: int
    length: int
    payload: bytes = b""

    def __post_init__(self) -> None:
        self.report_id = _byte(self.report_id, "report id")
        self.length = _byte(self.length, "length")
        _require(self.length >= 2,
                 f"a report frame is at least 2 bytes long (got {self.length})")
        self.payload = bytes(self.payload)
        _require(len(self.payload) == self.length - 2,
                 f"payload must be {self.length - 2} bytes in a {self.length}-byte frame, "
                 f"got {len(self.payload)}")

    @classmethod
    def from_bytes(cls, buf, report_id: int, length: int) -> "RawReport":
        raw = bytes(buf)
        _require(len(raw) == length,
                 f"report {report_id:#04x} must be {length} bytes, got {len(raw)}")
        _require(raw[0] == report_id,
                 f"expected report id {report_id:#04x}, got {raw[0]:#04x}")
        return cls(report_id=report_id, length=length, payload=raw[2:])

    def build(self) -> bytearray:
        buf = bytearray(self.length)
        buf[0] = self.report_id
        buf[1] = self.length
        buf[2:] = self.payload
        return buf

    def hex(self) -> str:
        return " ".join(f"{byte:02x}" for byte in self.payload)

    @property
    def text(self) -> str:
        """Payload rendered as text; bytes outside printable ASCII become dots."""
        return "".join(chr(byte) if 0x20 <= byte < 0x7F else "." for byte in self.payload)

    def as_dict(self) -> dict:
        return {
            "report_id": self.report_id,
            "length": self.length,
            "payload_hex": self.hex(),
            "payload_text": self.text,
        }


def parse_version(buf) -> RawReport:
    """Frame the 0x0B version report (see VERSION_NOTES)."""
    return RawReport.from_bytes(buf, RID_VERSION, VERSION_LENGTH)


def parse_profile_blob(buf) -> RawReport:
    """Frame the 0x0A profile blob (see PROFILE_BLOB_NOTES)."""
    return RawReport.from_bytes(buf, RID_PROFILE, PROFILE_LENGTH)


def parse_wake_report(buf) -> RawReport:
    """Frame the 0x07 wake/vibration report (see WAKE_UP_MODE_NOTES)."""
    return RawReport.from_bytes(buf, RID_WAKE, WAKE_LENGTH)
