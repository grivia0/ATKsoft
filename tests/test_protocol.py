"""Codec rules that were settled against the live mouse.

Every frame below was captured from the dongle (``atksoft raw <rid>``) and is
replayed through the codecs here: decode it, re-encode it with the codec that
would write it back, and the bytes have to come out unchanged -- apart from
byte[1], which is the firmware's length echo on a read and the vendor's packet
length on a write (see ``session._same_frame``).

These are the tests that would have caught the button slots and the profile
grant parameter being read from the wrong place, which is exactly what the fake
device in :mod:`tests.conftest` cannot tell on its own: it can only replay what
the codecs already produce.
"""

from __future__ import annotations

import pytest

from atksoft import protocol as p

#: 52-byte answer to a granted read of 0x04 (the declared length is 56, so the
#: four trailing bytes the firmware builds never make it across the wire).
DPI_FRAME = bytes.fromhex(
    "04 3a 01 00 00 3f 00 01 25 17 1f 3f 6f 07 00 00"
    "00 00 00 00 00 02 00 00 02 ff 00 00 00 ff 00 00"
    "00 ff ff ff 00 00 ff ff ff 00 ff ff 40 00 ff ff"
    "ff 01 0e 88")

#: 13-byte answer to a granted read of 0x05.
LIGHT_FRAME = bytes.fromhex("05 11 01 50 03 a8 00 ff 00 01 04 01 ff")

#: 9-byte answer to a granted read of 0x06 (1000 Hz).
POLL_FRAME = bytes.fromhex("06 0b 01 01 fe 00 00 00 00")

#: 59-byte answer to a granted read of 0x08 -- the frame that exposed the slot
#: order: dpi-cycle/dpi-plus/dpi-minus sit in *consecutive* slots 4/5/6.
BUTTONS_FRAME = bytes.fromhex(
    "08 3d 01 02 00 00 03 00 00 04 00 00 0d 00 00 0e"
    "00 00 0f 00 00 06 00 00 05 00 00 3c 00 00 01 00"
    "00 01 00 00 01 00 00 01 00 00 01 00 00 01 00 00"
    "01 00 00 0a 00 00 09 00 00 00 94")

#: 6-byte answer to a granted read of 0x0C (12 bytes on the device, cut off by
#: the descriptor).  Only the first four payload bytes carry the two fields.
PROFILE_FRAME = bytes.fromhex("0c 0c 01 fe 01 fe")

GOLDEN = (
    ("dpi", p.RID_DPI, DPI_FRAME, p.DpiSettings),
    ("lighting", p.RID_LIGHT, LIGHT_FRAME, p.LightingSettings),
    ("polling", p.RID_POLL, POLL_FRAME, p.PollingRate),
    ("buttons", p.RID_BUTTONS, BUTTONS_FRAME, p.ButtonSettings),
    ("profile", p.RID_PROFILE_SET, PROFILE_FRAME, p.ProfileSettings),
)


@pytest.mark.parametrize("key, rid, frame, codec", GOLDEN,
                         ids=[case[0] for case in GOLDEN])
def test_captured_frame_survives_decode_and_encode(mouse, key, rid, frame, codec):
    """What the dongle sent must be exactly what we would send back."""
    session, _ = mouse
    settings = codec.from_bytes(session._padded(rid, frame))
    rebuilt = bytes(session.frame_for(settings))

    assert len(rebuilt) == len(frame), f"{key}: encode length"
    assert rebuilt[:1] == frame[:1], f"{key}: report id"
    assert rebuilt[2:] == frame[2:], f"{key}: payload (byte[1] is the echo)"


def test_captured_button_frame_maps_slots_in_write_order():
    """dpi-cycle/dpi-plus/dpi-minus in slots 4/5/6 -- no 6/7/8 reversal."""
    settings = p.ButtonSettings.from_bytes(BUTTONS_FRAME, verify=True)

    actions = [settings.slot(number).action_name for number in range(1, 10)]
    assert actions == ["left-click", "right-click", "middle-click", "dpi-cycle",
                       "dpi-plus", "dpi-minus", "forward", "backward", "mode-key"]
    assert all(settings.slot(number).action_name == "disabled"
               for number in range(10, 17))
    assert [settings.slot(17).action_name, settings.slot(18).action_name] == \
        ["scroll-down", "scroll-up"]
    assert settings.profile == 1


def test_captured_profile_frame_decodes_current_and_maximum():
    settings = p.ProfileSettings.from_bytes(
        PROFILE_FRAME.ljust(p.PROFILE_SET_LENGTH, b"\x00"))

    assert settings.current == 1
    assert settings.maximum == 1


def test_profile_frame_shifted_by_a_grant_parameter_is_rejected():
    """A non-zero byte[4] makes the firmware insert itself as payload byte[2].

    This is the frame the dongle answers with while a 0x0C read is granted with
    the default parameter (0x01): the two profile fields have moved one byte to
    the right, and the reader's checksums are how that gets noticed.
    """
    shifted = bytes.fromhex("0c 0c 01 01 fe 01").ljust(p.PROFILE_SET_LENGTH, b"\x00")

    with pytest.raises(p.ChecksumError, match="current profile"):
        p.ProfileSettings.from_bytes(shifted)


def test_profile_grant_parameter_matches_the_layout_the_reader_expects():
    assert p.GRANT_PARAMETER_DEFAULT[p.RID_PROFILE_SET] == 0x00
    assert p.GRANT_PARAMETER_DEFAULT.get(p.RID_DPI, p.GRANT_DEFAULT_PARAMETER) == 0x01


def test_reply_length_echoes_the_declared_length_plus_two():
    assert p.reply_length(DPI_FRAME) == p.READ_GRANT_LENGTH[p.RID_DPI] + 2 == 58
    assert p.reply_length(PROFILE_FRAME) == p.READ_GRANT_LENGTH[p.RID_PROFILE_SET] + 2
    assert p.reply_length(bytes.fromhex("a0 01 00")) is None   # a status, not a length
    assert p.reply_length(b"") is None


# --------------------------------------------------------- the LED mode table

def test_light_modes_are_the_vendor_enum():
    """0x05/0x06 light the LED from the active DPI stage -- they are not "ripple".

    Both vendor drivers for this family (attack-shark-x11-driver's ``LightMode``
    enum and mousectl's ``LIGHT_MODES``) agree on the whole table:
    Off/Static/Breathing/Neon/ColorBreathing/StaticDpi/BreathingDpi.  The last two
    are the only modes whose colour comes from report 0x04's stages, so naming
    them something else quietly hides the one setting that keeps the eight stage
    colours on the LED instead of blinking past them.
    """
    assert p.LIGHT_MODES == {
        "off": 0x00,
        "steady": 0x01,          # vendor: Static
        "breathing": 0x02,
        "neon": 0x03,
        "rainbow": 0x04,         # vendor: ColorBreathing
        "steady-dpi": 0x05,      # vendor: StaticDpi
        "breathing-dpi": 0x06,   # vendor: BreathingDpi
    }
    assert p.MODE_BY_VALUE[0x05] == "steady-dpi"
    assert p.MODE_BY_VALUE[0x06] == "breathing-dpi"
    assert p.DPI_LIGHT_MODES == frozenset(("steady-dpi", "breathing-dpi"))


@pytest.mark.parametrize("name, wire", [("steady-dpi", 0x50), ("breathing-dpi", 0x60)])
def test_a_dpi_light_mode_is_the_high_nibble_of_byte_three(name, wire):
    """The mode is the high nibble of byte 3, and survives a rebuild."""
    built = p.LightingSettings(mode=p.LIGHT_MODES[name]).build()

    assert built[p.LIGHT_MODE_OFFSET] == wire
    assert p.LightingSettings.from_bytes(built).mode_name == name


def test_captured_lighting_frame_came_from_a_dpi_mode():
    """The dongle's own 0x05 answer carries 0x50 in byte 3: StaticDpi (mode 5)."""
    settings = p.LightingSettings.from_bytes(LIGHT_FRAME.ljust(p.LIGHT_LENGTH, b"\x00"))

    assert settings.mode_name == "steady-dpi"
    assert settings.mode == p.LIGHT_MODES["steady-dpi"] == 0x05


# ------------------------------------------------ the DPI indicator byte (49)

def test_the_dpi_indicator_byte_is_a_one_shot_animation():
    """Byte 49 is the vendor's "DPI Active Indicator" -- not a stage number.

    The vendor's own comment on the byte says so, and its reply parser calls the
    field ``dpiIndicationType``.  Only three values reach the firmware: no
    animation, three quick blinks, or a few seconds solid.  Reading a stage index
    into it would have made the "the LED blinks stage N" claim look like protocol
    instead of folklore.
    """
    assert p.DPI_INDICATOR_OFFSET == 49
    assert p.dpi_indicator_effect(p.DPI_INDICATOR_NONE) == "no animation"
    assert p.dpi_indicator_effect(p.DPI_INDICATOR_BLINK) == "three quick blinks"
    assert p.dpi_indicator_effect(p.DPI_INDICATOR_FLASH) == "solid for about three seconds"
    assert p.dpi_indicator_effect(3) == "ignored by the firmware"
    assert p.dpi_indicator_effect(0xFF) == "ignored by the firmware"


def test_the_vendor_default_frame_carries_the_vendor_flash():
    """The default report the vendor's builder writes stores 2 in byte 49."""
    settings = p.DpiSettings.from_bytes(p.DPI_VENDOR_DEFAULT)

    assert p.DPI_VENDOR_DEFAULT[p.DPI_INDICATOR_OFFSET] == p.DPI_INDICATOR_FLASH == 2
    assert settings.indicator == p.DPI_INDICATOR_FLASH


@pytest.mark.parametrize("value", [0, 1, 2, 9, 0xFF])
def test_the_dpi_indicator_byte_survives_a_rebuild(value):
    """ATKsoft keeps byte 49 verbatim: whatever the mouse holds goes back."""
    settings = p.DpiSettings(indicator=value)

    assert settings.build()[p.DPI_INDICATOR_OFFSET] == value
    assert p.DpiSettings.from_bytes(settings.build()).indicator == value
