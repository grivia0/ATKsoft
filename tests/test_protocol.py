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
