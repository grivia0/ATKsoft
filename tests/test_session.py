"""Transport rules: what goes on the wire, and what comes back.

No hardware: :mod:`tests.conftest` supplies the fake hidraw device.  The
interesting property under test is the length mismatch -- ``protocol`` builds
the vendor's packet sizes while the descriptor declares something shorter.
"""

from __future__ import annotations

import pytest

from atksoft import protocol as p
from atksoft import session as s


# ------------------------------------------------------- write path: trim

def test_write_is_trimmed_to_the_descriptor(mouse, features, build_frame):
    """A 56-byte vendor buffer goes out as the 52 bytes the wire takes."""
    session, device = mouse
    frame = session.write_dpi(p.DpiSettings())

    assert len(frame) == features[p.RID_DPI] + 1 == 52
    assert device.writes[-1] == frame
    assert bytes(frame) == build_frame(p.DpiSettings(), "dpi")[:52]
    assert frame[0] == p.RID_DPI


def test_lighting_write_is_trimmed_too(mouse, features, build_frame):
    session, device = mouse
    frame = session.write_lighting(p.LightingSettings())

    assert len(frame) == features[p.RID_LIGHT] + 1 == 13
    assert bytes(frame) == build_frame(p.LightingSettings(), "lighting")[:13]


def test_dpi_checksum_lives_inside_the_wire_frame(mouse, features):
    """The trim cannot cut the checksum off, or the firmware rejects the write."""
    frame = bytes(p.DpiSettings().build())[:features[p.RID_DPI] + 1]
    expected = int.from_bytes(frame[p.DPI_CHECKSUM_OFFSET:p.DPI_CHECKSUM_OFFSET + 2], "big")

    assert expected == p.checksum16(frame, 3, p.DPI_CHECKSUM_OFFSET)


def test_reports_that_match_the_codec_are_untouched(mouse):
    session, _ = mouse
    frame = session.write_polling(p.PollingRate(hz=500))

    assert len(frame) == p.POLL_LENGTH
    assert list(frame) == list(p.PollingRate(hz=500).build())


def test_empty_write_is_refused(mouse):
    session, _ = mouse
    with pytest.raises(s.SessionError, match="empty"):
        session.write_report(b"")


# -------------------------------------------------------- read path: pad

def test_read_is_padded_to_the_codec(mouse):
    """A 52-byte answer becomes the 56 bytes ``DpiSettings`` insists on."""
    session, _ = mouse
    settings = session.read_dpi()

    assert isinstance(settings, p.DpiSettings)
    assert settings.dpi == p.DpiSettings().dpi
    assert len(session.snapshot[p.RID_DPI]) == 52


def test_every_settings_report_round_trips(mouse):
    """Trim on the way out, pad on the way in: the values survive."""
    session, _ = mouse
    for key, rid, codec, writer, reader in (
        ("dpi", p.RID_DPI, p.DpiSettings, "write_dpi", "read_dpi"),
        ("lighting", p.RID_LIGHT, p.LightingSettings, "write_lighting", "read_lighting"),
        ("polling", p.RID_POLL, p.PollingRate, "write_polling", "read_polling"),
        ("buttons", p.RID_BUTTONS, p.ButtonSettings, "write_buttons", "read_buttons"),
        ("profile", p.RID_PROFILE_SET, p.ProfileSettings, "write_profile_settings",
         "read_profile_settings"),
    ):
        sent = codec()
        getattr(session, writer)(sent)
        back = getattr(session, reader)()
        assert isinstance(back, codec), key
        assert type(back) is codec


# ------------------------------------------------- read path: stale buffers

def test_a_stale_buffer_is_read_again(mouse_factory):
    """The firmware answers the status first and fills the report afterwards.

    A first ``GET_FEATURE`` can therefore still hand over the leftovers of an
    earlier, *shorter* grant of the same report -- the one stale answer the
    report id alone cannot expose, because byte[1] still echoes that older
    length.  The session has to notice and poll the buffer again, not return it.
    """
    session, device = mouse_factory()
    good = bytes(device.replies[p.RID_DPI])
    stale = bytearray(good[:8])
    stale[1] = 0x0C                      # left over from a 10-byte grant
    device.stale[p.RID_DPI] = [stale]
    grants = len(device.writes)

    settings = session.read_dpi()

    frame = bytes(session.snapshot[p.RID_DPI])
    assert len(device.writes) == grants + 1        # polled again, not granted again
    assert len(frame) == session.transfer_length(p.RID_DPI) == 52
    assert frame[1] == p.READ_GRANT_LENGTH[p.RID_DPI] + 2      # not the leftover 0x0C
    assert frame[2:] == good[2:len(frame)]                     # ...this answer's payload
    assert settings.dpi == p.DpiSettings().dpi


def test_a_buffer_that_never_settles_is_reported(mouse_factory):
    """A leftovers frame must not reach a codec as if it were the answer."""
    session, device = mouse_factory(retries=0)
    stale = bytearray(bytes(device.replies[p.RID_DPI])[:8])
    stale[1] = 0x0C
    device.stale[p.RID_DPI] = [stale] * 4

    with pytest.raises(s.SessionError, match="the buffer still held a 12-byte answer"):
        session.read_dpi()


# ------------------------------------------------------- transfer lengths

def test_transfer_length_comes_from_the_descriptor(mouse):
    session, _ = mouse
    assert session.transfer_length(p.RID_DPI) == 52
    assert session.transfer_length(p.RID_LIGHT) == 13


def test_undeclared_report_falls_back_to_the_codec_length(mouse_factory):
    """A descriptor that forgets 0x0b must not break the read."""
    session, _ = mouse_factory(features={p.RID_DPI: 51})
    assert session.transfer_length(p.RID_VERSION) == p.VERSION_LENGTH


def test_grant_report_has_a_length_even_when_undeclared(mouse_factory):
    """The 0xA0 handshake must never be the thing that fails."""
    session, _ = mouse_factory(features={p.RID_DPI: 51})
    assert session.transfer_length(p.RID_READ) == p.GRANT_LENGTH


def test_unknown_report_has_no_length(mouse):
    session, _ = mouse
    with pytest.raises(s.SessionError, match="no known vendor length"):
        session.transfer_length(0x11)


# --------------------------------------------------------- node discovery

def test_is_config_node_needs_both_markers(mouse_factory):
    session, _ = mouse_factory()
    assert s.is_config_node(session.node)

    input_only, _ = mouse_factory(features={p.RID_DPI: 51})
    assert not s.is_config_node(input_only.node)


def test_list_nodes_puts_the_config_interface_first(monkeypatch, mouse_factory):
    """hidrawN is not stable across replugs, so order comes from the descriptor."""
    plain, _ = mouse_factory(features={0x01: 6})
    plain.node.path = "/dev/hidraw0"
    config, _ = mouse_factory()
    config.node.path = "/dev/hidraw2"
    stranger, _ = mouse_factory(vid=0x1234)
    stranger.node.path = "/dev/hidraw9"

    monkeypatch.setattr(s, "enumerate_hidraw",
                        lambda: [stranger.node, plain.node, config.node])
    found = s.list_nodes()

    assert [node.path for node in found] == ["/dev/hidraw2", "/dev/hidraw0"]


def test_find_config_node_rejects_an_input_interface(monkeypatch, mouse_factory):
    """Only interface 2 carries 0x04 + 0xa0 on this dongle."""
    plain, _ = mouse_factory(features={0x01: 6})
    monkeypatch.setattr(s, "enumerate_hidraw", lambda: [plain.node])

    with pytest.raises(s.SessionError, match="hidraw_fake"):
        s.find_config_node()


def test_find_config_node_honours_an_explicit_path(monkeypatch, mouse_factory):
    config, _ = mouse_factory()
    monkeypatch.setattr(s, "enumerate_hidraw", lambda: [config.node])

    with pytest.raises(s.SessionError, match="is not a supported mouse"):
        s.find_config_node(node="/dev/hidraw7")


def test_open_session_passes_the_node_through(monkeypatch, mouse_factory):
    session, _ = mouse_factory()
    asked = []

    def fake_find(node=None):
        asked.append(node)
        return session.node

    monkeypatch.setattr(s, "find_config_node", fake_find)
    monkeypatch.setattr(s, "HidrawDevice", lambda path: session.device)

    opened = s.open_session(node="/dev/hidraw2", settle=0.0, grant_delay=0.0)

    assert asked == ["/dev/hidraw2"]
    assert opened.node is session.node
    assert opened.settle == 0.0


# ------------------------------------------------------- the 0xA0 handshake

def test_grant_request_goes_out_before_the_read(mouse):
    """SET_FEATURE a0 ... then GET_FEATURE a0, then the payload -- in that order."""
    session, device = mouse
    session.read_dpi()

    assert len(device.writes) == 1
    grant = device.writes[0]
    assert len(grant) == p.GRANT_LENGTH
    assert bytes(grant) == bytes(p.build_grant_request(
        p.RID_DPI, p.GRANT_DEFAULT_PARAMETER, p.READ_GRANT_LENGTH[p.RID_DPI]))
    assert grant[0] == p.RID_READ
    assert grant[1] == p.RID_DPI


def test_grant_declares_the_vendor_length_not_the_wire_length(mouse_factory):
    """0x04 declares 0x38 bytes even though the descriptor transfers 52."""
    session, device = mouse_factory()
    session.read_dpi()

    assert device.writes[0][2] == 0x38
    assert session.transfer_length(p.RID_DPI) == 52


def test_refused_grant_raises(mouse_factory):
    session, device = mouse_factory(grant=False)

    with pytest.raises(s.SessionError, match="refused the read request"):
        session.read_dpi()
    assert device.writes, "the request is still sent, it is simply refused"


def test_profile_grant_carries_the_parameter_the_layout_needs(mouse):
    """0x0C shifts its answer when byte[4] is non-zero, so it has to go out as 0.

    The default 0x01 inserts itself as payload byte[2] and the fields land one
    byte to the right of where ``ProfileSettings`` reads them.
    """
    session, device = mouse

    session.read_profile_settings()

    grant = [frame for frame in device.writes if frame[0] == p.RID_READ][-1]
    assert grant == bytes(p.build_grant_request(
        p.RID_PROFILE_SET, p.GRANT_PARAMETER_DEFAULT[p.RID_PROFILE_SET],
        p.READ_GRANT_LENGTH[p.RID_PROFILE_SET]))
    assert grant[4] == 0x00


def test_every_other_read_keeps_the_vendor_parameter(mouse):
    session, device = mouse

    session.read_dpi()
    session.read_buttons()

    params = [frame[4] for frame in device.writes if frame[0] == p.RID_READ]
    assert params == [p.GRANT_DEFAULT_PARAMETER] * 2


def test_the_length_echo_does_not_make_a_report_look_changed(mouse):
    """A reply is never byte-equal to a write frame: byte[1] differs by design.

    The firmware answers with the declared read length + 2 there (see
    ``protocol.reply_length``) while ``build()`` writes the vendor's packet
    length, so a naive ``snapshot == frame_for(...)`` compares a length against
    a length and calls every report dirty.
    """
    session, device = mouse
    state = session.read_settings()
    written = session.frame_for(state["dpi"])

    assert session.snapshot[p.RID_DPI][1] == p.READ_GRANT_LENGTH[p.RID_DPI] + 2
    assert session.snapshot[p.RID_DPI][1] != written[1]
    assert session.snapshot[p.RID_DPI][2:] == bytes(written)[2:]

    before = len(device.writes)
    assert session.apply(state) == []
    assert len(device.writes) == before


def test_read_of_an_undeclared_report_is_refused(mouse_factory):
    session, _ = mouse_factory(features={p.RID_DPI: 51})

    with pytest.raises(s.SessionError, match="does not expose"):
        session.read_report(p.RID_LIGHT)


def test_writing_to_an_undeclared_report_is_refused(mouse_factory):
    session, _ = mouse_factory(features={p.RID_DPI: 51})

    with pytest.raises(s.SessionError, match="does not accept writes"):
        session.write_lighting(p.LightingSettings())


def test_macro_read_needs_the_full_frame(mouse_factory):
    """The descriptor transfers 64 bytes for a report that is 131 bytes long."""
    session, _ = mouse_factory()

    with pytest.raises(s.SessionError, match="macro report"):
        session.read_macro()


# ------------------------------------------------------------- read_settings

def test_read_settings_only_returns_what_the_interface_exposes(mouse_factory):
    session, _ = mouse_factory(features={p.RID_DPI: 51, p.RID_LIGHT: 12, p.RID_READ: 7})

    state = session.read_settings()

    assert set(state) == {"dpi", "lighting"}


def test_read_settings_can_be_narrowed(mouse):
    session, device = mouse
    state = session.read_settings(keys=["polling"])

    assert set(state) == {"polling"}
    assert state["polling"].hz in p.POLL_RATES


# ------------------------------------------------------------------- apply()

def test_apply_writes_nothing_when_nothing_changed(mouse):
    session, device = mouse
    state = session.read_settings()
    before = len(device.writes)

    assert session.apply(state) == []
    assert len(device.writes) == before


def test_apply_writes_the_changed_report_and_then_the_lighting(mouse):
    """A DPI write drags report 0x05 along; the tests below say why."""
    session, device = mouse
    state = session.read_settings()
    state["dpi"].set_stage_dpi(1, 400)
    before = len(device.writes)

    results = session.apply(state)

    assert [rid for rid, _ in results] == [p.RID_DPI, p.RID_LIGHT]
    written = [frame for frame in device.writes[before:] if frame[0] == p.RID_DPI]
    assert len(written) == 1
    assert len(written[0]) == 52
    assert 400 in _decoded_stage_dpis(written[0])


def test_a_dpi_write_is_followed_by_report_0x05(mouse):
    """The LED only moves when 0x05 follows 0x04.

    Report 0x04's byte 49 is a one-shot animation and the steady colour belongs
    to report 0x05, so a stage change that leaves 0x05's own bytes untouched
    still has to re-send it -- which is how both vendor drivers write the pair.
    """
    session, device = mouse
    state = session.read_settings()
    state["dpi"].current_stage = 3
    before = len(device.writes)

    results = session.apply(state)

    assert [rid for rid, _ in results] == [p.RID_DPI, p.RID_LIGHT]
    assert [frame[0] for frame in device.writes[before:] if frame[0] != p.RID_READ] == \
        [p.RID_DPI, p.RID_LIGHT]
    assert isinstance(results[-1][1], p.LightingSettings)


def test_the_lighting_repeat_echoes_what_the_device_holds(mouse):
    """The repeat is a re-send, not an edit: the frame matches the last read."""
    session, device = mouse
    state = session.read_settings()
    state["dpi"].current_stage = 3
    before = len(device.writes)

    session.apply(state)

    repeat = [frame for frame in device.writes[before:] if frame[0] == p.RID_LIGHT][-1]
    assert bytes(repeat) == bytes(session.frame_for(state["lighting"]))


def test_the_lighting_repeat_re_reads_when_only_the_dpi_was_handed_over(mouse):
    """``atksoft set dpi`` and the GUI's DPI tab pass report 0x04 alone."""
    session, device = mouse
    settings = session.read_dpi()
    settings.current_stage = 3
    before = len(device.writes)

    results = session.apply({"dpi": settings}, keys=["dpi"])

    assert [rid for rid, _ in results] == [p.RID_DPI, p.RID_LIGHT]
    assert [frame[0] for frame in device.writes[before:] if frame[0] != p.RID_READ] == \
        [p.RID_DPI, p.RID_LIGHT]
    assert isinstance(results[-1][1], p.LightingSettings)


def test_a_lighting_edit_does_not_drag_the_dpi_report_along(mouse):
    """The pair only runs one way: only a DPI write needs 0x05 behind it."""
    session, device = mouse
    state = session.read_settings()
    state["lighting"].rgb = (0x11, 0x22, 0x33)
    before = len(device.writes)

    results = session.apply(state)

    assert [rid for rid, _ in results] == [p.RID_LIGHT]
    assert [frame[0] for frame in device.writes[before:] if frame[0] != p.RID_READ] == \
        [p.RID_LIGHT]


def test_a_dpi_write_on_an_interface_without_report_0x05(mouse_factory):
    """A device that exposes 0x04 only still gets its report, and nothing else."""
    session, device = mouse_factory(features={p.RID_DPI: 51, p.RID_READ: 7})
    settings = session.read_dpi()
    settings.current_stage = 3
    before = len(device.writes)

    results = session.apply({"dpi": settings}, keys=["dpi"])

    assert [rid for rid, _ in results] == [p.RID_DPI]
    assert [frame[0] for frame in device.writes[before:] if frame[0] != p.RID_READ] == \
        [p.RID_DPI]


def test_apply_can_return_the_fresh_read(mouse):
    session, _ = mouse
    state = session.read_settings()
    state["profile"].current = 2

    results = session.apply(state)

    assert results and results[0][0] == p.RID_PROFILE_SET
    assert isinstance(results[0][1], p.ProfileSettings)


def _decoded_stage_dpis(frame) -> list:
    """Decode the stage table of a 52-byte wire frame, without the checksum."""
    settings = p.DpiSettings.from_bytes(bytes(frame).ljust(p.DPI_LENGTH, b"\x00"),
                                        verify=False)
    return settings.dpi


# ------------------------------------------------ interrupt-IN events

def test_events_decode_battery_reports_and_ignore_input(mouse, event_report):
    session, device = mouse
    device.queue = [
        bytes((0x01, 0x02, 0x03)),                      # an ordinary mouse report
        event_report(p.MESSAGE_BATTERY, p.BATTERY_NORMAL, 0x64),
        event_report(p.MESSAGE_BATTERY, p.BATTERY_NORMAL, 0x58),
    ]

    events = session.events(0.05)

    assert len(events) == 2
    assert all(isinstance(event, p.BatteryEvent) for event in events)
    assert events[0].percent == 0x64
    assert events[-1].percent == 0x58


def test_battery_keeps_the_last_report_of_the_window(mouse, event_report):
    """The first report after connecting is a stale 100%."""
    session, device = mouse
    device.queue = [event_report(p.MESSAGE_BATTERY, p.BATTERY_NORMAL, 100),
                    event_report(p.MESSAGE_BATTERY, p.BATTERY_NORMAL, 88)]

    event = session.battery(timeout=0.05)

    assert event is not None
    assert event.percent == 88
    assert event.state_name == "discharging"
    assert not event.charging


def test_battery_returns_none_when_nothing_arrives(mouse):
    session, _ = mouse
    assert session.battery(timeout=0.0, attempts=1) is None


def test_battery_reports_the_charging_state(mouse, event_report):
    session, device = mouse
    device.queue = [event_report(p.MESSAGE_BATTERY, p.BATTERY_CHARGING, 42)]

    event = session.battery(timeout=0.05)

    assert event.charging and event.state_name == "charging"


def test_watch_yields_until_its_deadline(mouse, event_report):
    session, device = mouse
    device.queue = [event_report(p.MESSAGE_PROFILE_CHANGED, 2)]   # device counts from 0

    seen = list(session.watch(seconds=0.05, timeout=0.01))

    assert [type(event).__name__ for event in seen] == ["ProfileChangedEvent"]
    assert seen[0].profile == 3                                   # ...so this is profile 3
