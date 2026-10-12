"""The GUI, driven headless.

The window is exercised against the same :class:`tests.conftest.FakeDevice` the
rest of the suite uses, with ``DeviceLink(threaded=False)`` so a request runs
inline and a test can assert on the result the moment it returns.  One test
(:func:`test_threaded_link_delivers_responses`) runs the real threaded plumbing.

Qt needs a platform plugin even with no display, so ``QT_QPA_PLATFORM`` is set
to ``offscreen`` here -- before any Qt object exists -- unless the environment
already asks for something else.
"""

from __future__ import annotations

import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtWidgets import QApplication                # noqa: E402

from atksoft import gui                                 # noqa: E402
from atksoft import protocol as p                       # noqa: E402

#: The descriptor the live X6 dongle reports (see tests/conftest.py).
LIVE_FEATURES = {0x04: 51, 0x05: 12, 0x06: 8, 0x07: 7, 0x08: 58,
                 0x09: 63, 0x0A: 127, 0x0B: 7, 0x0C: 5, 0xA0: 7}


@pytest.fixture(scope="module")
def qt_app():
    """One QApplication for the whole module (Qt allows no more than one)."""
    return QApplication.instance() or QApplication(["atksoft-gui-test"])


def pump(app, predicate, timeout: float = 5.0) -> bool:
    """Spin the event loop until ``predicate`` holds (for the threaded test)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


class _Built:
    """A window plus the pieces behind it, so a test can reach any of them."""

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)

    def writes_to(self, rid: int) -> list:
        """Every frame written to report ``rid`` (grants excluded)."""
        return [bytes(frame) for frame in self.device.writes if frame[0] == rid]

    @property
    def grants(self) -> list:
        return [bytes(frame) for frame in self.device.writes if frame[0] == p.RID_READ]


@pytest.fixture
def build_window(qt_app, mouse_factory):
    """``build_window(profile=1, connect=True, **make_mouse_kwargs)``.

    Returns a :class:`_Built` with the window, its link, the worker, the fake
    device and the session.  Every link built here is stopped at teardown, so a
    leaked worker thread cannot outlive its test.
    """
    made = []

    def builder(profile: int = 1, connect: bool = True, **kwargs):
        session, device = mouse_factory(**kwargs)
        worker = gui.MouseWorker(session=session)
        link = gui.DeviceLink(worker, threaded=False)
        window = gui.MainWindow(link, profile=profile)
        made.append(link)
        if connect:
            window.connect_to_device()
        return _Built(window=window, link=link, worker=worker, device=device,
                      session=session)

    try:
        yield builder
    finally:
        for link in made:
            link.stop()


def padded(rid: int, frame) -> bytearray:
    """A transfer frame at the length the codec for ``rid`` insists on."""
    length = {0x04: p.DPI_LENGTH, 0x05: p.LIGHT_LENGTH, 0x06: p.POLL_LENGTH,
              0x08: p.BUTTON_LENGTH, 0x0C: p.PROFILE_SET_LENGTH}[rid]
    raw = bytearray(bytes(frame)[:length])
    raw.extend(b"\x00" * (length - len(raw)))
    return raw


# ------------------------------------------------------------------ the window

def test_window_without_a_device_is_inert(qt_app):
    """No node, no session: every tab says so and nothing can be saved."""
    link = gui.DeviceLink(threaded=False)
    window = gui.MainWindow(link)
    try:
        assert window.statusBar().currentMessage() == "pick a node and press Connect"
        for key, page in window.pages.items():
            assert not page._available, key
            assert not page.save_button.isEnabled(), key
            assert not page.revert_button.isEnabled(), key
        assert window.dpi_panel.dpi_spins[0].value() == 0
        assert window.device_panel.descriptor_table.rowCount() == 0
    finally:
        link.stop()


def test_connect_reads_every_report_into_the_tabs(build_window):
    """The five settings reports land in the tabs they belong to."""
    built = build_window()
    window = built.window

    assert sorted(window.state) == ["buttons", "dpi", "lighting", "polling", "profile"]
    assert window.dpi_panel.dpi_spins[0].value() == 800
    assert window.dpi_panel.dpi_spins[1].value() == 1600
    assert window.dpi_panel.enable_boxes[0].isChecked()
    assert window.lighting_panel.mode_combo.currentText() == "off"
    assert window.device_panel.polling_combo.currentText() == "1000 Hz"
    assert window.device_panel.current_spin.value() == 1
    assert all(not page._dirty for page in window.pages.values())
    assert all(page.status.text() == "matches the mouse"
               for page in window.pages.values())
    # The descriptor inventory is on screen, not just in the session.
    assert window.device_panel.descriptor_table.rowCount() == len(LIVE_FEATURES)
    assert "1d57:fa60" in window.device_panel.device_label.text()


def test_saving_an_untouched_tab_writes_nothing(build_window):
    """Load -> collect -> apply has to be a no-op, or the GUI rewrites the mouse.

    This is the test that catches a widget quietly normalising a value on load:
    the frame rebuilt from the tab would differ from the one the firmware sent.
    """
    built = build_window()
    window = built.window
    before = list(built.device.writes)

    for key, page in window.pages.items():
        window.save_page(page)
        assert list(built.device.writes) == before, key
        assert page.status.text() == "already matches the mouse"
    assert window.statusBar().currentMessage() == "nothing changed -- no frame was sent"


def test_editing_one_dpi_stage_writes_one_frame(build_window):
    """One edit, one frame, and every other byte of the report as it was read."""
    built = build_window()
    window = built.window
    before = window.state["dpi"]
    window.dpi_panel.dpi_spins[0].setValue(1650)
    assert window.pages["dpi"]._dirty and window.pages["dpi"].save_button.isEnabled()

    window.save_page(window.pages["dpi"])

    frames = built.writes_to(p.RID_DPI)
    assert len(frames) == 1
    assert len(frames[0]) == LIVE_FEATURES[p.RID_DPI] + 1     # trimmed to the wire
    written = p.DpiSettings.from_bytes(padded(p.RID_DPI, frames[0]))
    assert written.stage_dpi(1) == 1650
    assert written.stage_dpi(1) != before.stage_dpi(1)
    assert written.dpi[1:] == before.dpi[1:]                  # nothing else moved
    assert written.current_stage == before.current_stage
    assert written.active_stages == before.active_stages
    assert written.colors == before.colors
    # The only companion is report 0x05: the re-send that puts the active
    # stage's colour on the LED (see tests/test_session.py).
    assert [frame[0] for frame in built.device.writes if frame[0] != p.RID_READ] == \
        [p.RID_DPI, p.RID_LIGHT]
    assert built.grants, "the write is a read-modify-write, so a grant came first"


def test_clicking_a_stage_radio_makes_that_stage_current(build_window):
    """The *Current* column is how the mouse's DPI stage gets switched.

    Qt moves the check mark on its own, so a radio that nobody watches leaves
    the tab clean and Save disabled: the click looks like it worked and never
    reaches the mouse.
    """
    built = build_window()
    window = built.window
    before = window.state["dpi"]

    window.dpi_panel.active_group.button(4).click()

    assert window.pages["dpi"]._dirty
    assert window.pages["dpi"].save_button.isEnabled()
    window.save_page(window.pages["dpi"])

    frames = built.writes_to(p.RID_DPI)
    assert len(frames) == 1
    written = p.DpiSettings.from_bytes(padded(p.RID_DPI, frames[0]))
    assert written.current_stage == 4
    assert written.current_stage != before.current_stage
    assert written.dpi == before.dpi                    # nothing else moved
    assert written.colors == before.colors
    assert written.active_stages == before.active_stages
    assert written.lift_off_distance == before.lift_off_distance


def test_a_reloaded_current_stage_does_not_dirty_the_dpi_tab(build_window):
    """Following the mouse's DPI button must not become an unsaved edit.

    Loading a tab whose radio has to move (the device is on another stage now)
    unchecks the old radio, and that second ``toggled`` would otherwise mark the
    freshly loaded tab dirty -- with a Save button that writes a stage back to a
    mouse that is already on it.
    """
    built = build_window()
    window = built.window
    window.state["dpi"].current_stage = 4

    window.pages["dpi"].load(window.state)

    assert window.dpi_panel.active_group.checkedId() == 4
    assert not window.pages["dpi"]._dirty
    assert window.pages["dpi"].status.text() == "matches the mouse"
    assert not window.pages["dpi"].save_button.isEnabled()


def test_an_off_table_dpi_value_is_announced(build_window):
    """The user is told what the sensor will store before the write happens."""
    built = build_window()
    built.window.dpi_panel.dpi_spins[0].setValue(1655)
    note = built.window.dpi_panel.note.text()
    assert "1655" in note and "store" in note


def test_the_dpi_tab_says_which_led_modes_use_the_stage_colours(build_window):
    """A stage colour that "does not change the light" is the firmware working.

    The LED is report 0x05's single colour in every mode but two, so the tab has
    to point at the modes that do take their colour from the active DPI stage --
    otherwise editing a colour here looks like it did nothing at all.
    """
    built = build_window()
    window = built.window
    assert window.lighting_panel.mode_combo.currentText() == "off"

    note = window.dpi_panel.note.text()
    assert "steady-dpi" in note and "breathing-dpi" in note and "off" in note

    # On one of those modes the colours *are* the LED colour: nothing to say.
    window.state["lighting"].mode = p.LIGHT_MODES["steady-dpi"]
    window.pages["dpi"].load(window.state)

    assert window.dpi_panel.note.text() == ""


def test_the_indicator_spin_says_what_byte_49_really_is(build_window):
    """The DPI tab's indicator byte is the confirmation animation, not a stage.

    It used to say the vendor publishes no meaning for the byte; the vendor's own
    comment on it reads "DPI Active Indicator", so the tooltip has to name the
    one-shot animation and say outright that it is not a stage number.
    """
    built = build_window()
    spin = built.window.dpi_panel.indicator_spin

    assert "byte 49" in spin.toolTip()
    assert "confirmation animation" in spin.toolTip()
    assert "not a stage number" in spin.toolTip()
    assert spin.value() == p.DPI_INDICATOR_FLASH == 2      # what the vendor stores


def test_the_led_hint_points_at_the_confirmation_animation(build_window):
    """The tab's notice explains the flash instead of promising a colour change."""
    built = build_window()
    note = built.window.dpi_panel.note.text()

    assert "confirmation animation" in note
    assert "blinks the new stage's colour" not in note


def test_a_sensor_warning_does_not_swallow_the_led_hint(build_window):
    """Both notices matter: the snap warning wins, then the hint comes back."""
    built = build_window()
    panel = built.window.dpi_panel

    panel.dpi_spins[0].setValue(1655)
    assert "store" in panel.note.text()

    panel.dpi_spins[0].setValue(1650)          # on the sensor's table again
    assert "steady-dpi" in panel.note.text()


def test_picking_a_dpi_mode_says_the_colour_stops_applying(build_window):
    """The swatch is dead in the two DPI modes, and the tab says so at once."""
    built = build_window()
    panel = built.window.lighting_panel
    assert panel.note.text() == ""

    panel.mode_combo.setCurrentText("steady-dpi")

    assert "active DPI stage" in panel.note.text()
    assert built.window.pages["lighting"]._dirty


def test_editing_a_button_binding_round_trips(build_window):
    """Binding specs are the CLI's: what is typed is what the frame carries."""
    built = build_window()
    window = built.window
    window.buttons_panel.edits[3].setText("key:shift+a")
    assert not window.buttons_panel.problems()

    window.save_page(window.pages["buttons"])

    frames = built.writes_to(p.RID_BUTTONS)
    assert len(frames) == 1
    written = p.ButtonSettings.from_bytes(padded(p.RID_BUTTONS, frames[0]))
    slot = written.slot(4)
    assert slot.action == p.ACTION_BY_NAME["keyboard-key"]
    assert slot.usage == p.key_usage("a")
    assert slot.modifiers == p.MODIFIERS["shift"]
    # The display form is the writer's, and it parses back into the same slot --
    # which is what makes "copy this row into atksoft set buttons" work.
    assert p.format_action_spec(slot) == "key:a+shift"
    assert p.parse_action_spec(p.format_action_spec(slot)) == slot


def test_a_broken_binding_never_reaches_the_wire(build_window):
    """A row that does not parse blocks the whole tab, with the reason shown."""
    built = build_window()
    window = built.window
    before = list(built.device.writes)
    window.buttons_panel.edits[3].setText("key:")

    assert window.buttons_panel.problems()
    window.save_page(window.pages["buttons"])

    assert list(built.device.writes) == before
    assert "not written" in window.pages["buttons"].status.text()
    assert "unknown key" in window.statusBar().currentMessage()
    assert window.pages["buttons"]._dirty          # the edit is still there to fix


def test_the_read_only_button_slot_cannot_be_edited(build_window):
    """Slot 5 is read-only in the vendor tool too (protocol.READ_ONLY_SLOTS)."""
    built = build_window()
    panel = built.window.buttons_panel
    assert not panel.edits[4].isEnabled()
    assert not panel.pickers[4].isEnabled()
    assert panel.edits[3].isEnabled()


def test_a_report_the_link_lacks_greys_its_tab(build_window):
    """A dongle without report 0x05 says so instead of showing zeros."""
    features = {rid: length for rid, length in LIVE_FEATURES.items()
                if rid != p.RID_LIGHT}
    built = build_window(features=features)
    window = built.window

    assert "lighting" not in window.state
    assert not window.lighting_panel.isEnabled()
    assert "05" in window.lighting_panel.note.text()
    assert not window.pages["lighting"].save_button.isEnabled()
    assert window.pages["lighting"].status.text() == "no editable report on this link"
    # The reports that are there still loaded.
    assert window.dpi_panel.dpi_spins[0].value() == 800


def test_a_refused_grant_is_reported_verbatim(build_window):
    """No state, no silent zeros: the status bar carries the firmware's answer."""
    built = build_window(grant=False)
    window = built.window

    assert window.state == {}
    assert "refused the read request" in window.statusBar().currentMessage()
    assert not window.pages["dpi"].save_button.isEnabled()
    assert "error" in window.monitor_panel.log.toPlainText()


# ----------------------------------------------------------------- raw reports

def test_raw_read_then_write_the_same_frame(build_window):
    """The Advanced tab: read a report, copy it back, write it."""
    built = build_window()
    window = built.window
    panel = window.advanced_panel

    panel.rid_edit.setText("06")
    panel._do_read()
    assert panel._last and panel._last[0] == p.RID_POLL
    assert "read 0x06" in panel.output.toPlainText()
    assert "0000  06" in panel.output.toPlainText()

    panel._do_fill()
    assert panel.write_edit.toPlainText().startswith("06 ")
    assert not panel.write_button.isEnabled()       # needs the acknowledgement

    panel.understand_box.setChecked(True)
    assert panel.write_button.isEnabled()
    panel._do_write()

    assert built.device.writes[-1] == panel._last
    assert "wrote 0x06" in panel.output.toPlainText()


def test_raw_write_needs_a_parseable_frame(build_window):
    """Nonsense in the text box is refused before it can become a frame."""
    built = build_window()
    panel = built.window.advanced_panel
    panel.understand_box.setChecked(True)
    before = list(built.device.writes)

    panel.write_edit.setPlainText("not hex at all")
    panel._do_write()
    assert list(built.device.writes) == before
    assert "cannot write" in panel.output.toPlainText()

    panel.write_edit.setPlainText("   ")
    panel._do_write()
    assert list(built.device.writes) == before
    assert "the frame is empty" in panel.output.toPlainText()


def test_revert_throws_away_unsaved_edits(build_window):
    """Revert restores the device's own values without touching the wire."""
    built = build_window()
    window = built.window
    before = list(built.device.writes)
    window.dpi_panel.dpi_spins[0].setValue(1650)
    assert window.pages["dpi"]._dirty

    window.revert_page(window.pages["dpi"])

    assert window.dpi_panel.dpi_spins[0].value() == 800
    assert not window.pages["dpi"]._dirty
    assert window.pages["dpi"].status.text() == "reverted to what the mouse reported"
    assert list(built.device.writes) == before


def test_reset_to_factory_only_fills_the_table(build_window):
    """*Reset to factory mapping* is a table edit, not a write."""
    built = build_window()
    window = built.window
    before = list(built.device.writes)

    window.buttons_panel.reset_button.click()
    assert window.buttons_panel.edits[0].text() == "left-click"
    assert window.buttons_panel.edits[5].text() == "dpi-cycle"
    assert window.buttons_panel.edits[16].text() == "scroll-up"
    assert window.pages["buttons"]._dirty
    assert list(built.device.writes) == before


def test_the_colour_button_takes_a_new_colour(build_window, monkeypatch):
    """The swatch button is the only way to set a colour, so it gets a test."""
    from PyQt6.QtGui import QColor

    built = build_window()
    panel = built.window.dpi_panel
    assert panel.colors[0].rgb() == (0xFF, 0x00, 0x00)

    monkeypatch.setattr(gui.QColorDialog, "getColor",
                        staticmethod(lambda *a, **k: QColor(0x12, 0x34, 0x56)))
    panel.colors[0]._pick()
    assert panel.colors[0].rgb() == (0x12, 0x34, 0x56)
    assert built.window.pages["dpi"]._dirty

    monkeypatch.setattr(gui.QColorDialog, "getColor",
                        staticmethod(lambda *a, **k: QColor()))
    panel.colors[1]._pick()                     # an invalid colour is ignored
    assert panel.colors[1].rgb() == (0x00, 0xFF, 0x00)


def test_the_quick_raw_buttons_read_the_undecoded_reports(build_window):
    """0x0b/0x07/0x0a/0x09 all read, and each lands in the Advanced output."""
    built = build_window()
    panel = built.window.advanced_panel
    for button, (rid, _label) in zip(panel.quick_buttons, gui.RAW_BUTTONS):
        panel.output.clear()
        button.click()
        assert f"read {rid:#04x}" in panel.output.toPlainText()
        assert panel._last[0] == rid


def test_the_advanced_tab_can_copy_the_last_frame(build_window):
    """Copying puts the same hex on the clipboard that the read showed."""
    built = build_window()
    panel = built.window.advanced_panel
    panel._do_copy()
    assert "nothing has been read" in panel.output.toPlainText()

    panel.rid_edit.setText("06")
    panel._do_read()
    panel._do_copy()
    expected = " ".join(f"{byte:02x}" for byte in panel._last)
    assert gui.QApplication.clipboard().text() == expected


def test_connecting_to_a_node_that_is_not_there_says_so(qt_app):
    """A typo'd --node is an error message, not a traceback."""
    link = gui.DeviceLink(threaded=False)        # no session: the real search runs
    window = gui.MainWindow(link, node="/dev/hidraw99")
    try:
        window.connect_to_device()
        message = window.statusBar().currentMessage()
        assert "/dev/hidraw99" in message and "not a supported mouse" in message
        assert window.state == {}
        assert not window.pages["dpi"].save_button.isEnabled()
    finally:
        link.stop()


# --------------------------------------------------------------------- events

def test_events_stream_into_the_monitor(build_window, event_report):
    """Watch mode renders every event kind, and stopping it is immediate."""
    built = build_window()
    window = built.window
    built.device.queue.extend([
        event_report(p.MESSAGE_DPI_CYCLE, param1=3),
        event_report(p.MESSAGE_PROFILE_CHANGED, param1=1),
        event_report(p.MESSAGE_BATTERY, param1=p.BATTERY_NORMAL, param2=87),
        event_report(p.MESSAGE_COMMAND_CONFIRMATION, param1=p.COMMAND_OK, param2=0x06),
    ])
    built.link.eventFired.connect(lambda *_: built.link.stop_watch())
    built.link.watch()                              # inline: returns when stopped

    log = window.monitor_panel.log.toPlainText()
    assert "dpi stage -> 3" in log
    assert "profile -> 2" in log
    assert "battery: 87%" in log
    assert "firmware confirmed a write to report 0x06" in log
    assert window.monitor_panel.count_label.text() == "4 events"
    assert window.battery_label.text() == "battery 87%"
    window.monitor_panel.clear()
    assert window.monitor_panel.log.toPlainText() == ""


def test_the_mouse_dpi_button_moves_the_current_radio(build_window, event_report):
    """The DPI button moves the stage without the app, and the tab follows it.

    Nothing is written -- the mouse is already on the new stage -- so the tab has
    to stay clean while the radio and the stored state move with the mouse.
    """
    built = build_window()
    window = built.window
    assert window.dpi_panel.active_group.checkedId() == 2
    before = list(built.device.writes)

    built.device.queue.append(event_report(p.MESSAGE_DPI_CYCLE, param1=5))
    built.link.eventFired.connect(lambda *_: built.link.stop_watch())
    built.link.watch()                              # inline: returns when stopped

    assert window.dpi_panel.active_group.checkedId() == 5
    assert window.state["dpi"].current_stage == 5
    assert not window.pages["dpi"]._dirty
    assert window.pages["dpi"].status.text() == "matches the mouse"
    assert not window.pages["dpi"].save_button.isEnabled()
    assert "stage 5" in window.statusBar().currentMessage()
    assert list(built.device.writes) == before      # following the mouse is not a write


def test_a_dpi_button_stage_the_tab_does_not_have_is_ignored(build_window, event_report):
    """A stage number no radio carries must not move the selection."""
    built = build_window()
    window = built.window

    built.device.queue.append(event_report(p.MESSAGE_DPI_CYCLE, param1=0))
    built.link.eventFired.connect(lambda *_: built.link.stop_watch())
    built.link.watch()

    assert window.dpi_panel.active_group.checkedId() == 2
    assert window.state["dpi"].current_stage == 2
    assert "stage 0" not in window.statusBar().currentMessage()


def test_battery_event_reaches_the_device_tab(build_window, event_report):
    """The battery read is an event window; the result lands on the Device tab."""
    built = build_window()
    built.device.queue.append(
        event_report(p.MESSAGE_BATTERY, param1=p.BATTERY_FULLY_CHARGED, param2=100))
    built.link.battery()

    assert "100%" in built.window.device_panel.battery_label.text()
    assert built.window.statusBar().currentMessage() == "battery: 100% (full)"
    assert built.window.battery_label.text() == "battery 100%"


def test_battery_without_an_event_says_so(build_window):
    """No event is not an error, but it is worth a sentence."""
    built = build_window()
    built.link.battery()
    assert "no event" in built.window.device_panel.battery_label.text()
    assert "no battery event" in built.window.statusBar().currentMessage()


def test_event_lines_for_every_known_event():
    """The formatter is the one place that decides how an event reads."""
    assert gui._format_event({"event": "battery", "percent": 42,
                              "state_name": "charging", "charging": True}) \
        == "battery: 42% (charging) -- on the cable"
    assert gui._format_event({"event": "dpi-cycle", "stage": 5}) == "dpi stage -> 5"
    assert gui._format_event({"event": "profile-changed", "profile": 3}) \
        == "profile -> 3"
    assert gui._format_event({"event": "vibration-mode", "mode_name": "enhanced"}) \
        == "vibration mode -> enhanced"
    assert gui._format_event({"event": "command-confirmation", "ok": False,
                              "report_id": 0x04}) \
        == "firmware REFUSED a write to report 0x04"
    assert gui._format_event({"event": "unknown", "code": 0x99}).startswith(
        "unknown event:")


# ------------------------------------------------------------------ threading

def test_threaded_link_delivers_responses(qt_app, mouse_factory):
    """The production wiring: queued requests, a worker thread, same behaviour."""
    session, device = mouse_factory()
    worker = gui.MouseWorker(session=session)
    link = gui.DeviceLink(worker, threaded=True)
    window = gui.MainWindow(link)
    try:
        window.connect_to_device()
        assert pump(qt_app, lambda: bool(window.state)), "no state arrived"
        assert window.dpi_panel.dpi_spins[0].value() == 800
        assert window.node_label.text().startswith(session.path)

        window.dpi_panel.dpi_spins[0].setValue(1200)
        window.save_page(window.pages["dpi"])
        assert pump(qt_app, lambda: any(frame[0] == p.RID_DPI
                                        for frame in device.writes))
        assert pump(qt_app, lambda: not window.busy_label.text())
        assert "saved dpi" in window.statusBar().currentMessage()
    finally:
        link.stop()
    assert not worker._watching


def test_stopping_the_link_is_idempotent(qt_app, mouse_factory):
    """Closing the window (Qt may do it twice) must not raise."""
    session, device = mouse_factory()
    link = gui.DeviceLink(gui.MouseWorker(session=session), threaded=False)
    window = gui.MainWindow(link)
    window.close()
    link.stop()
    link.stop()
    assert not device.opened


def test_the_key_picker_lists_and_filters_keys(qt_app):
    """The dialog is only ever built from a button, so a test builds it here.

    A missing import in this class would otherwise only show up as a traceback
    in someone's terminal the first time they clicked *Key…*.
    """
    dialog = gui.KeyPickerDialog()
    try:
        assert dialog.list.count() > 100          # the whole HID keyboard page
        names = [dialog.list.item(row).text() for row in range(dialog.list.count())]
        assert any(name.startswith("Enter") for name in names)

        dialog.search.setText("enter")
        visible = [dialog.list.item(row).text() for row in range(dialog.list.count())
                   if not dialog.list.item(row).isHidden()]
        assert visible == ["Enter   (0x28)", "Keypad Enter   (0x58)"]
        assert dialog.usage() == 0x28

        dialog.search.setText("no such key")
        assert all(dialog.list.item(row).isHidden()
                   for row in range(dialog.list.count()))
    finally:
        dialog.deleteLater()


def test_picking_a_key_writes_a_spec_the_parser_accepts(build_window, monkeypatch):
    """*Key…* keeps the row's modifiers and produces a spec that parses back."""
    built = build_window()
    panel = built.window.buttons_panel
    panel.edits[3].setText("key:ctrl+0x39")        # caps lock, held with ctrl

    class FakePicker:
        """Stands in for the modal dialog: the user picked F5."""

        def __init__(self, parent=None):
            pass

        def exec(self):
            return gui.QDialog.DialogCode.Accepted

        def usage(self):
            return p.key_usage("F5")

    monkeypatch.setattr(gui, "KeyPickerDialog", FakePicker)
    panel._pick_key(3)

    slot = p.parse_action_spec(panel.edits[3].text())
    assert slot.action == p.ACTION_BY_NAME["keyboard-key"]
    assert slot.usage == p.key_usage("F5")
    assert slot.modifiers == p.MODIFIERS["ctrl"]   # the row's modifier survived
    # format_action_spec keeps the display name's case, and the spec still parses.
    assert panel.edits[3].text() == "key:F5+ctrl"
    assert p.parse_action_spec(panel.edits[3].text()) == slot


def test_a_cancelled_key_picker_changes_nothing(build_window, monkeypatch):
    """Dismissing the dialog leaves the row exactly as it was."""
    built = build_window()
    panel = built.window.buttons_panel
    panel.edits[3].setText("fire")

    class CancelPicker:
        def __init__(self, parent=None):
            pass

        def exec(self):
            return gui.QDialog.DialogCode.Rejected

        def usage(self):
            raise AssertionError("usage() must not be consulted after a cancel")

    monkeypatch.setattr(gui, "KeyPickerDialog", CancelPicker)
    panel._pick_key(3)
    assert panel.edits[3].text() == "fire"


# ------------------------------------------------------------------- packaging

def test_the_parser_exposes_the_documented_options():
    """`atksoft gui --help` is the CLI's contract with the user."""
    args = gui._parser().parse_args(["--profile", "3", "--no-connect"])
    assert args.profile == 3 and args.no_connect and args.node is None


def test_the_cli_gui_command_hands_over_to_this_module(monkeypatch):
    """`atksoft gui` parses its own flags and calls gui.main with them."""
    from atksoft import cli

    args = cli.build_parser().parse_args(["gui", "--profile", "2", "--no-connect",
                                          "--node", "/dev/hidraw7"])
    assert args.func is cli.cmd_gui
    seen = {}

    def fake_main(argv):
        seen["argv"] = list(argv)
        return 0

    monkeypatch.setattr(gui, "main", fake_main)
    assert cli.cmd_gui(args) == 0
    assert seen["argv"] == ["--profile", "2", "--node", "/dev/hidraw7", "--no-connect"]


def test_a_missing_qt_is_reported_with_the_fix(monkeypatch):
    """The one place that mentions the extra has to mention pip as well.

    ``atksoft gui`` in a fresh process imports ``atksoft.gui`` for the first
    time, so a box without PyQt6 raises ``ModuleNotFoundError`` out of that
    import.  Once this module *has* been imported, ``from . import gui`` finds it
    as an attribute of the package and never asks the import system at all --
    which is why the test drops the attribute and blocks the finder rather than
    faking ``sys.modules``.
    """
    import sys

    import atksoft
    from atksoft import cli
    from atksoft.session import SessionError

    class NoQt:
        """Stand in for the missing PyQt6: atksoft.gui cannot be imported."""

        def find_spec(self, name, path=None, target=None):
            if name == "atksoft.gui":
                raise ModuleNotFoundError("No module named 'PyQt6'")
            return None

    monkeypatch.delattr(atksoft, "gui", raising=False)
    monkeypatch.delitem(sys.modules, "atksoft.gui", raising=False)
    monkeypatch.setattr(sys, "meta_path", [NoQt(), *sys.meta_path])

    args = cli.build_parser().parse_args(["gui", "--no-connect"])
    with pytest.raises(SessionError, match=r"atksoft\[gui\]"):
        cli.cmd_gui(args)

