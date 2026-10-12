"""``atksoft.gui`` -- the PyQt6 control panel.

The window is a thin skin over :mod:`atksoft.session`, and deliberately so: it
can only ever send bytes a codec in :mod:`atksoft.protocol` produced from a
report the device just handed over.  A tab loads the decoded dataclass, edits
it in place and gives it back to :meth:`session.MouseSession.apply`, so the
read-modify-write rule the CLI lives by is the same rule here -- there is no
second writer to keep in sync.

Two things differ from the CLI:

*   **The node stays open.**  The CLI opens a node per command; the window keeps
    one :class:`~atksoft.session.MouseSession` for its lifetime, so a second
    atksoft process (or the vendor tool) can hold the same dongle at the same
    time and interleave its ``0xA0`` grants with ours -- the one failure a
    per-report check cannot see.  Reads therefore verify the length the grant
    asked for, the watchdog in :class:`MouseWorker` re-reads after a write, and
    a refused grant is reported verbatim rather than dressed up.
*   **Every session call runs on a worker thread.**  One read is a grant, a
    0.25 s sleep and a transfer; a reload is five of them.  Doing that on the
    GUI thread would freeze the window for a second at a time, so widgets only
    emit *requests* through :class:`DeviceLink` and render *responses* -- no
    widget in this module ever touches a file descriptor or sleeps.

Deliberately absent, because the underlying report is not understood well
enough to write safely (see ``protocol.MACRO_WRITE_NOTES``, the notes on 0x07
and 0x0A, and the body of :data:`protocol.MACRO_WRITE_NOTES`): macro writes,
wake-up mode and the profile blob.  The *Advanced* tab reads and writes any
report verbatim and says so in the loudest label in the file.
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import datetime
import sys

try:                                            # pragma: no cover - see below
    from PyQt6.QtCore import QObject, QThread, Qt, pyqtSignal, pyqtSlot
    from PyQt6.QtGui import QColor, QFont
    from PyQt6.QtWidgets import (QAbstractItemView, QApplication, QButtonGroup,
                                 QCheckBox, QColorDialog, QComboBox, QDialog,
                                 QDialogButtonBox, QFormLayout, QGroupBox,
                                 QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                                 QListWidget, QListWidgetItem, QMainWindow,
                                 QPlainTextEdit, QPushButton, QRadioButton,
                                 QScrollArea, QSlider, QSpinBox, QTabWidget,
                                 QTableWidget, QTableWidgetItem, QToolBar,
                                 QVBoxLayout, QWidget)
except ModuleNotFoundError as exc:              # pragma: no cover - needs Qt
    raise ModuleNotFoundError(
        "atksoft.gui needs PyQt6 -- install it with: pip install 'atksoft[gui]'"
    ) from exc

from . import keynames
from . import protocol as p
from .cli import format_color, hexdump, parse_hex, parse_report_id
from .session import (REPORT_NAMES, MouseSession, SessionError, is_config_node,
                      list_nodes, open_session)

__all__ = ["MouseWorker", "DeviceLink", "MainWindow", "main"]

#: Reports the *Advanced* tab offers a one-click read for, with the reason they
#: are only ever read.
RAW_BUTTONS = (
    (p.RID_VERSION, "version (0x0b)"),
    (p.RID_WAKE, "wake-up mode (0x07)"),
    (p.RID_PROFILE, "profile blob (0x0a)"),
    (p.RID_MACRO, "macro (0x09)"),
)


@contextlib.contextmanager
def _quiet(*widgets):
    """Fill widgets programmatically without firing "the user edited this"."""
    for widget in widgets:
        widget.blockSignals(True)
    try:
        yield
    finally:
        for widget in widgets:
            widget.blockSignals(False)


def _mono(widget):
    """Give ``widget`` the fixed-width font, so hexdumps line up."""
    font = QFont("monospace")
    font.setStyleHint(QFont.StyleHint.Monospace)
    widget.setFont(font)
    return widget


def _rgb(color: QColor) -> tuple:
    return (color.red(), color.green(), color.blue())


class ColorButton(QPushButton):
    """A push button whose face is the colour it edits."""

    #: the user picked a different colour
    colorChanged = pyqtSignal(object)

    def __init__(self, rgb=(0, 0, 0), parent=None):
        super().__init__(parent)
        self.setFixedWidth(96)
        self._rgb = tuple(rgb)
        self.set_rgb(self._rgb)
        self.clicked.connect(self._pick)

    def rgb(self) -> tuple:
        return self._rgb

    def set_rgb(self, rgb) -> None:
        self._rgb = tuple(rgb)
        self.setStyleSheet(f"background-color: {format_color(self._rgb)};")

    def _pick(self) -> None:
        chosen = QColorDialog.getColor(QColor(*self._rgb), self, "Pick a colour")
        if chosen.isValid() and _rgb(chosen) != self._rgb:
            self.set_rgb(_rgb(chosen))
            self.colorChanged.emit(self._rgb)


# ============================================================ device worker
# Everything that talks to the mouse lives on this object.  Its slots are
# ordinary methods, so a test can call them straight away; production connects
# them to the request signals on DeviceLink instead.

class MouseWorker(QObject):
    """One :class:`~atksoft.session.MouseSession`, one operation at a time.

    ``session`` exists for tests: pass a
    :class:`~atksoft.session.MouseSession` built on a ``FakeDevice`` and nothing
    in here touches hardware.  Without it the worker opens the real thing in
    :meth:`open_node`.
    """

    #: node path, capabilities (see :meth:`capabilities`); after a successful open
    opened = pyqtSignal(str, object)
    #: decoded settings ``{key: codec}``, capabilities
    loaded = pyqtSignal(object, object)
    #: what ``apply`` did: keys, the frames that would go out, report ids written
    applied = pyqtSignal(object)
    #: report id, transfer bytes, bytes the kernel reported moving (may be None)
    rawRead = pyqtSignal(int, object, object)
    #: a frame that really went out
    wrote = pyqtSignal(object)
    #: human-readable failure
    failed = pyqtSignal(str)
    #: busy, what is being done ("" when idle)
    busyChanged = pyqtSignal(bool, str)
    #: one decoded interrupt-IN event, as a dict
    eventFired = pyqtSignal(object)
    #: a BatteryEvent or None
    batteryRead = pyqtSignal(object)
    #: the watch loop started/stopped
    watchState = pyqtSignal(bool)

    def __init__(self, session: MouseSession | None = None, parent=None):
        super().__init__(parent)
        self.session = session
        self._owns_session = session is None
        #: last complete read, so an apply only has to refresh what it wrote
        self._state: dict = {}
        self._caps: dict = {}
        self.profile = 1
        self._watching = False

    # -- lifetime ---------------------------------------------------------

    @pyqtSlot(str)
    def open_node(self, path: str = "") -> None:
        """Find/open the configuration interface and report what it carries."""
        self.busyChanged.emit(True, "looking for the mouse…")
        try:
            if self.session is None:
                self.session = open_session(path or None)
                self._owns_session = True
            self.session.device.open()
            self._caps = self.capabilities()
        except (SessionError, p.ProtocolError, OSError) as error:
            self.failed.emit(str(error))
            self.busyChanged.emit(False, "")
            return
        self.opened.emit(self.session.path, self._caps)
        self.busyChanged.emit(False, "")

    def capabilities(self) -> dict:
        """What this link exposes: the descriptor inventory and the settings map.

        The GUI shows this table because "the button report is missing" and "the
        mouse is asleep" look identical from a failed read otherwise.
        """
        node = self.session.node
        return {
            "path": self.session.path,
            "pid": node.pid,
            "interface": node.interface,
            "name": node.name,
            "features": {rid: length for rid, length in sorted(node.features.items())},
            "supported": {key: self.session.supports(rid)
                          for key, rid in MouseSession.SETTINGS_REPORTS},
        }

    def stop_watch(self) -> None:
        """Ask the watch loop to return (a plain flag, safe from any thread)."""
        self._watching = False

    def close(self) -> None:
        """Close the node.  Called once the worker thread has stopped."""
        if self.session is not None:
            with contextlib.suppress(OSError):
                self.session.device.close()

    # -- reads ------------------------------------------------------------

    @pyqtSlot(int, object)
    def reload(self, profile: int = 1, keys=None) -> None:
        """Read every settings report this link exposes and emit them."""
        self.profile = int(profile or 1)
        self.busyChanged.emit(True, f"reading profile {self.profile}…")
        try:
            state = self.session.read_settings(profile=self.profile, keys=keys)
        except (SessionError, p.ProtocolError, OSError) as error:
            self.failed.emit(str(error))
            self.busyChanged.emit(False, "")
            return
        self._state = dict(state) if keys is None else {**self._state, **state}
        self.loaded.emit(dict(self._state), self._caps)
        self.busyChanged.emit(False, "")

    @pyqtSlot(int, object, object)
    def read_raw(self, rid: int, declared_length=None, parameter=None) -> None:
        """Read one report verbatim, whatever the codecs make of it."""
        self.busyChanged.emit(True, f"reading report {rid:#04x}…")
        try:
            frame = bytes(self.session.read_report(
                rid, declared_length=declared_length, parameter=parameter))
            moved = self.session.last_read
        except (SessionError, p.ProtocolError, OSError) as error:
            self.failed.emit(str(error))
            self.busyChanged.emit(False, "")
            return
        self.rawRead.emit(rid, frame, moved)
        self.busyChanged.emit(False, "")

    @pyqtSlot()
    def poll_battery(self) -> None:
        """Listen for a battery event (only a wireless link ever sends one)."""
        self.busyChanged.emit(True, "waiting for a battery event…")
        event = None
        try:
            event = self.session.battery(timeout=1.2, attempts=2)
        except (SessionError, p.ProtocolError, OSError) as error:
            self.failed.emit(str(error))
        self.batteryRead.emit(event)
        self.busyChanged.emit(False, "")


    # -- writes -----------------------------------------------------------

    @pyqtSlot(object, object)
    def apply_state(self, payload, keys=None) -> None:
        """Write the edited reports, read back what changed, refresh the rest.

        The re-read is the point: the firmware normalises fields (a DPI stage
        snaps onto the sensor's own table, for one), so what is shown afterwards
        is what the mouse stores, not what was asked for.
        """
        keys = [key for key in (keys or list(payload)) if key in payload]
        self.busyChanged.emit(True, "writing " + ", ".join(keys) + "…")
        frames = {}
        try:
            for key in keys:
                frames[key] = bytes(self.session.frame_for(payload[key]))
            results = self.session.apply(payload, keys=keys)
        except (SessionError, p.ProtocolError, OSError) as error:
            self.failed.emit(str(error))
            self.busyChanged.emit(False, "")
            return
        self.applied.emit({
            "keys": keys,
            "frames": frames,
            "written": [rid for rid, _fresh in results],
        })
        if not results:
            # Nothing changed, so nothing went out -- and re-reading would spend
            # one of the firmware's single reads to learn that.
            self.busyChanged.emit(False, "")
            return
        try:
            fresh = self.session.read_settings(profile=self.profile, keys=keys)
        except (SessionError, p.ProtocolError, OSError) as error:
            # The write stands; only the re-read fell over.  Say so instead of
            # reporting the whole save as failed.
            self.failed.emit(f"written, but reading it back failed: {error}")
            self.busyChanged.emit(False, "")
            return
        self._state.update(fresh)
        self.loaded.emit(dict(self._state), self._caps)
        self.busyChanged.emit(False, "")

    @pyqtSlot(object)
    def write_raw(self, frame) -> None:
        """Send one frame exactly as given (the *Advanced* tab's escape hatch)."""
        self.busyChanged.emit(True, "writing a raw frame…")
        try:
            sent = bytes(self.session.write_report(frame))
        except (SessionError, p.ProtocolError, OSError) as error:
            self.failed.emit(str(error))
            self.busyChanged.emit(False, "")
            return
        self.wrote.emit(sent)
        self.busyChanged.emit(False, "")

    # -- events -----------------------------------------------------------

    @pyqtSlot()
    def start_watch(self) -> None:
        """Stream interrupt-IN events until :meth:`stop_watch`.

        The loop is deliberately short-windowed rather than one blocking
        ``session.watch()`` call: the flag is set from the GUI thread, and a
        generator parked in a long window would not notice for that long.
        """
        self._watching = True
        self.watchState.emit(True)
        self.busyChanged.emit(True, "watching for events…")
        try:
            while self._watching:
                for event in self.session.events(0.4):
                    self.eventFired.emit(event.as_dict())
        except (SessionError, p.ProtocolError, OSError) as error:
            self.failed.emit(str(error))
        finally:
            self._watching = False
            self.busyChanged.emit(False, "")
            self.watchState.emit(False)


# ============================================================== device link

class DeviceLink(QObject):
    """The window's only handle on the mouse: requests in, responses out.

    In production the worker is moved to a :class:`QThread` and every request
    below is delivered as a queued call, so the GUI thread never blocks in an
    ioctl.  ``threaded=False`` connects the same signals directly and runs the
    work inline -- that is the mode the test suite uses, where the "device" is a
    dict of canned answers and a thread would only add timing to the tests.
    """

    # requests (window -> worker)
    openRequested = pyqtSignal(str)
    reloadRequested = pyqtSignal(int, object)
    applyRequested = pyqtSignal(object, object)
    rawReadRequested = pyqtSignal(int, object, object)
    rawWriteRequested = pyqtSignal(object)
    watchRequested = pyqtSignal()
    batteryRequested = pyqtSignal()

    # responses (worker -> window), re-emitted so the window needs no worker
    opened = pyqtSignal(str, object)
    loaded = pyqtSignal(object, object)
    applied = pyqtSignal(object)
    rawRead = pyqtSignal(int, object, object)
    wrote = pyqtSignal(object)
    failed = pyqtSignal(str)
    busyChanged = pyqtSignal(bool, str)
    eventFired = pyqtSignal(object)
    batteryRead = pyqtSignal(object)
    watchState = pyqtSignal(bool)

    #: response signal names, so the wiring below stays in one place
    RESPONSES = ("opened", "loaded", "applied", "rawRead", "wrote", "failed",
                 "busyChanged", "eventFired", "batteryRead", "watchState")

    def __init__(self, worker: MouseWorker | None = None, *, threaded: bool = True,
                 parent=None):
        super().__init__(parent)
        self.worker = worker if worker is not None else MouseWorker()
        self.thread: QThread | None = None
        mode = (Qt.ConnectionType.QueuedConnection if threaded
                else Qt.ConnectionType.DirectConnection)
        if threaded:
            self.thread = QThread()
            self.worker.moveToThread(self.thread)
            self.thread.start()
        requests = (
            (self.openRequested, self.worker.open_node),
            (self.reloadRequested, self.worker.reload),
            (self.applyRequested, self.worker.apply_state),
            (self.rawReadRequested, self.worker.read_raw),
            (self.rawWriteRequested, self.worker.write_raw),
            (self.watchRequested, self.worker.start_watch),
            (self.batteryRequested, self.worker.poll_battery),
        )
        for request, slot in requests:
            request.connect(slot, mode)
        for name in self.RESPONSES:
            getattr(self.worker, name).connect(getattr(self, name), mode)

    # -- convenience, so the window has no signal syntax in it -------------

    def open(self, node: str = "") -> None:
        self.openRequested.emit(node or "")

    def reload(self, profile: int = 1, keys=None) -> None:
        self.reloadRequested.emit(int(profile), keys)

    def apply(self, payload, keys=None) -> None:
        self.applyRequested.emit(payload, list(keys) if keys else None)

    def read_raw(self, rid: int, declared_length=None, parameter=None) -> None:
        self.rawReadRequested.emit(int(rid), declared_length, parameter)

    def write_raw(self, frame) -> None:
        self.rawWriteRequested.emit(bytes(frame))

    def watch(self) -> None:
        self.watchRequested.emit()

    def stop_watch(self) -> None:
        """Ask a running watch loop to return before the next event window ends."""
        self.worker.stop_watch()

    def battery(self) -> None:
        self.batteryRequested.emit()

    def stop(self) -> None:
        """Stop the worker and close the node; safe to call twice."""
        self.worker.stop_watch()
        if self.thread is not None:
            self.thread.quit()
            self.thread.wait(3000)
            self.thread = None
        self.worker.close()


# ================================================================== panels
# A panel is one tab's worth of widgets over decoded reports.  It loads from
# the codec objects the device sent and writes back into codec objects it is
# handed a copy of, so a half-edited tab can always be reverted by loading the
# last read again.

def _select(combo: QComboBox, value, label=lambda value: str(value)) -> bool:
    """Select ``value`` in ``combo``, adding it when the table lacks it.

    A device that reports something the vendor's writer would reject (a
    brightness of 0, a sleep time off the LUT) must not lose that byte just
    because a combo box cannot show it: the value is added, so loading and
    immediately saving is a no-op.  Returns True when an item was added.
    """
    position = combo.findData(value)
    added = position < 0
    if added:
        combo.addItem(label(value), value)
        position = combo.count() - 1
    combo.setCurrentIndex(position)
    return added


class SettingsPanel(QWidget):
    """Base class for the tabs that edit ``session.read_settings`` reports.

    Subclasses implement :meth:`build`, :meth:`load` and :meth:`collect`.
    """

    #: the settings keys this panel edits
    keys: tuple = ()
    #: any user edit; the page turns this into "Save changes" becoming live
    changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._layout = QVBoxLayout(self)
        self.note = QLabel()
        self.note.setWordWrap(True)
        self.build()
        self._layout.addWidget(self.note)
        self._layout.addStretch(1)

    # -- to implement ------------------------------------------------------

    def build(self) -> None:
        """Create the widgets and add them to ``self._layout``."""
        raise NotImplementedError

    def load(self, state: dict) -> None:
        """Show ``state`` (device truth).  Missing keys mean "not exposed"."""
        raise NotImplementedError

    def collect(self, state: dict) -> list:
        """Write the widgets into ``state``'s codecs; return the keys to apply."""
        return list(self.keys)

    # -- helpers -----------------------------------------------------------

    def _watch(self, widget, *signals) -> None:
        """Report a user edit on ``widget`` (one or more signal names)."""
        for name in signals:
            getattr(widget, name).connect(lambda *_: self.changed.emit())

    def _say(self, text: str) -> None:
        """The one-line notice under the panel (empty text hides it)."""
        self.note.setText(text)
        self.note.setVisible(bool(text))

    def _absent(self, key: str) -> bool:
        """Note that this link does not carry ``key`` and grey the tab out."""
        rid = dict(MouseSession.SETTINGS_REPORTS).get(key)
        self.setEnabled(False)
        self._say(f"report {rid:#04x} ({key}) is not in this link's descriptor, "
                  f"so there is nothing to edit here")
        return True


class DpiPanel(SettingsPanel):
    """Report 0x04: the eight stages, their colours and the sensor switches."""

    keys = ("dpi",)

    def build(self) -> None:
        table = QTableWidget(p.MAX_DPI_STAGES, 4, self)
        table.setHorizontalHeaderLabels(["Current", "On", "DPI", "Colour"])
        # These colours are not what lights the LED in most modes, and saying
        # nothing about that reads as a broken tab: name the modes that do use
        # them (protocol.DPI_LIGHT_MODES) where they are edited.
        table.horizontalHeaderItem(3).setToolTip(
            "the colour the LED shows while the mouse is on a stage, in the LED "
            f"modes {p.DPI_LIGHT_MODES_TEXT}")
        #: the notice load() puts under the table, kept so that a DPI-snap
        #: warning can be shown without losing it
        self._hint = ""
        table.verticalHeader().setVisible(False)
        table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setMaximumHeight(240)
        table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.active_group = QButtonGroup(self)
        self.enable_boxes: list = []
        self.dpi_spins: list = []
        self.colors: list = []
        for index in range(p.MAX_DPI_STAGES):
            stage = index + 1
            radio = QRadioButton(table)
            radio.setToolTip(f"make stage {stage} the stage the mouse is on -- "
                             f"the DPI button cycles its stages from there")
            # Qt moves the check mark by itself, so without this connection a
            # click on the radio would look like it worked and never reach the
            # mouse: no edit, no dirty tab, no enabled Save.
            self._watch(radio, "toggled")
            self.active_group.addButton(radio, stage)
            table.setCellWidget(index, 0, radio)
            box = QCheckBox(table)
            self._watch(box, "toggled")
            table.setCellWidget(index, 1, box)
            spin = QSpinBox(table)
            spin.setRange(0, p.DPI_MAX)
            spin.setSingleStep(50)
            spin.setSuffix(" dpi")
            spin.setSpecialValueText("off")
            spin.valueChanged.connect(
                lambda value, number=stage: self._snapped(number, value))
            table.setCellWidget(index, 2, spin)
            button = ColorButton(p.DPI_STAGE_COLORS[index], table)
            button.setToolTip(f"stage {stage}: the colour the LED shows while the "
                              f"mouse is on stage {stage} (LED modes "
                              f"{p.DPI_LIGHT_MODES_TEXT})")
            button.colorChanged.connect(lambda *_: self.changed.emit())
            table.setCellWidget(index, 3, button)
            self.enable_boxes.append(box)
            self.dpi_spins.append(spin)
            self.colors.append(button)
        self._layout.addWidget(table)

        sensor = QGroupBox("Sensor", self)
        row = QHBoxLayout(sensor)
        self.lift_combo = QComboBox(sensor)
        for distance in p.LIFT_OFF_DISTANCES:
            self.lift_combo.addItem(f"lift-off {distance} mm", distance)
        self.angle_box = QCheckBox("angle snap", sensor)
        self.motion_box = QCheckBox("motion sync", sensor)
        self.ripple_box = QCheckBox("ripple control", sensor)
        self.indicator_spin = QSpinBox(sensor)
        self.indicator_spin.setRange(0, 0xFF)
        self.indicator_spin.setPrefix("indicator ")
        self.indicator_spin.setToolTip(
            "report 0x04 byte 49, kept verbatim: the LED's one-shot confirmation "
            "animation after a DPI write (0 none, 1 three quick blinks, 2 about "
            "three seconds solid, anything else ignored).  It is not a stage "
            "number -- the steady colour comes from Lighting's mode")
        for widget, signal in ((self.lift_combo, "currentIndexChanged"),
                               (self.angle_box, "toggled"),
                               (self.motion_box, "toggled"),
                               (self.ripple_box, "toggled"),
                               (self.indicator_spin, "valueChanged")):
            self._watch(widget, signal)
        for widget in (self.lift_combo, self.angle_box, self.motion_box,
                       self.ripple_box, self.indicator_spin):
            row.addWidget(widget)
        row.addStretch(1)
        self._layout.addWidget(sensor)

    def _snapped(self, stage: int, value: int) -> None:
        """Say what the PAW3311 will really store, when that differs."""
        stored = p.bytes_to_dpi(*p.dpi_to_bytes(value))
        warning = (f"stage {stage}: {value} dpi is not on the sensor's table -- "
                   f"the mouse will store {stored} dpi")
        self._say(warning if stored != value else self._hint)
        self.changed.emit()

    @staticmethod
    def _led_hint(lighting) -> str:
        """Why the stage colours may not be reaching the LED right now.

        Report 0x05 owns the LED's colour, and only two of its modes take that
        colour from the stage the mouse is on.  Anywhere else the LED keeps its
        own colour and a DPI change only plays report 0x04's one-shot
        confirmation animation, which makes a DPI change look like it changed
        nothing at all.
        """
        if lighting is None or lighting.mode_name in p.DPI_LIGHT_MODES:
            return ""
        return (f"the LED stays on these colours only while Lighting's mode is "
                f"{p.DPI_LIGHT_MODES_TEXT} -- in {lighting.mode_name} the LED keeps "
                f"its own colour and a DPI change only plays the confirmation "
                f"animation")

    def show_current_stage(self, stage: int) -> bool:
        """Move the *Current* radio to ``stage`` without calling it an edit.

        The mouse's own DPI button announces where it landed, and that is device
        truth arriving unasked -- the mouse is already on the new stage, so there
        is nothing to save and the tab has to stay clean.  Returns whether the
        radio actually moved.
        """
        try:
            button = self.active_group.button(int(stage))
        except (TypeError, ValueError):
            return False
        if button is None or button.isChecked():
            return False
        # Quiet across the whole group: checking one button unchecks the old one,
        # and that second ``toggled`` would otherwise look like an edit too.
        with _quiet(*self.active_group.buttons()):
            button.setChecked(True)
        return True

    def _widgets(self) -> tuple:
        return (self.lift_combo, self.angle_box, self.motion_box, self.ripple_box,
                self.indicator_spin, *self.enable_boxes, *self.dpi_spins,
                *self.colors, *self.active_group.buttons())

    def load(self, state: dict) -> None:
        settings = state.get("dpi")
        if settings is None:
            self._absent("dpi")
            return
        self.setEnabled(True)
        self._say("")
        with _quiet(*self._widgets()):
            for index in range(p.MAX_DPI_STAGES):
                stage = index + 1
                self.enable_boxes[index].setChecked(settings.stage_enabled(stage))
                self.dpi_spins[index].setValue(int(settings.stage_dpi(stage)))
                self.colors[index].set_rgb(settings.stage_color(stage))
            for button in self.active_group.buttons():
                if self.active_group.id(button) == int(settings.current_stage):
                    button.setChecked(True)
            _select(self.lift_combo, int(settings.lift_off_distance),
                    lambda value: f"lift-off {value} mm")
            self.angle_box.setChecked(bool(settings.angle_snap))
            self.motion_box.setChecked(bool(settings.motion_sync))
            self.ripple_box.setChecked(bool(settings.ripple_control))
            self.indicator_spin.setValue(int(settings.indicator))
        self._hint = self._led_hint(state.get("lighting"))
        self._say(self._hint)

    def collect(self, state: dict) -> list:
        settings = state["dpi"]
        for index in range(p.MAX_DPI_STAGES):
            stage = index + 1
            settings.set_stage_enabled(stage, self.enable_boxes[index].isChecked())
            settings.set_stage_dpi(stage, int(self.dpi_spins[index].value()))
            settings.set_stage_color(stage, self.colors[index].rgb())
        current = self.active_group.checkedId()
        if current >= 1:
            settings.current_stage = current
        settings.lift_off_distance = int(self.lift_combo.currentData())
        settings.angle_snap = self.angle_box.isChecked()
        settings.motion_sync = self.motion_box.isChecked()
        settings.ripple_control = self.ripple_box.isChecked()
        settings.indicator = int(self.indicator_spin.value())
        return list(self.keys)


class LightingPanel(SettingsPanel):
    """Report 0x05: LED mode and colour, the sleep timers and the debounce."""

    keys = ("lighting",)

    def build(self) -> None:
        led = QGroupBox("LED", self)
        form = QFormLayout(led)
        self.mode_combo = QComboBox(led)
        for name in p.LIGHT_MODES:
            self.mode_combo.addItem(name, p.LIGHT_MODES[name])
        self.color_button = ColorButton((0x00, 0xFF, 0x00), led)
        self.color_button.setToolTip(
            f"the LED's colour, except in the LED modes {p.DPI_LIGHT_MODES_TEXT}, "
            f"which light the LED in the active DPI stage's colour instead")
        self.brightness_slider = QSlider(Qt.Orientation.Horizontal, led)
        self.brightness_slider.setRange(p.BRIGHTNESS_MIN, p.BRIGHTNESS_MAX)
        self.speed_slider = QSlider(Qt.Orientation.Horizontal, led)
        self.speed_slider.setRange(p.LED_SPEED_MIN, p.LED_SPEED_MAX)
        self.speed_slider.setToolTip(f"{p.LED_SPEED_MAX} is the fastest")
        form.addRow("mode", self.mode_combo)
        form.addRow("colour", self.color_button)
        form.addRow(f"brightness ({p.BRIGHTNESS_MIN}..{p.BRIGHTNESS_MAX})",
                    self.brightness_slider)
        form.addRow(f"speed ({p.LED_SPEED_MIN}..{p.LED_SPEED_MAX})",
                    self.speed_slider)
        self._layout.addWidget(led)

        power = QGroupBox("Power", self)
        form = QFormLayout(power)
        self.sleep_combo = QComboBox(power)
        for minutes in p.SLEEP_CHOICES:
            self.sleep_combo.addItem(f"{minutes:g} min", minutes)
        self.sleep_combo.setToolTip("how long before the LED goes out")
        self.deep_spin = QSpinBox(power)
        self.deep_spin.setRange(1, p.DEEP_SLEEP_MINUTES_MAX)
        self.deep_spin.setSuffix(" min")
        self.deep_spin.setToolTip("how long before the mouse itself goes to sleep")
        self.debounce_combo = QComboBox(power)
        for milliseconds in p.DEBOUNCE_CHOICES:
            self.debounce_combo.addItem(f"{milliseconds} ms", milliseconds)
        form.addRow("light off after", self.sleep_combo)
        form.addRow("deep sleep after", self.deep_spin)
        form.addRow("debounce", self.debounce_combo)
        self._layout.addWidget(power)

        for widget, signal in ((self.mode_combo, "currentIndexChanged"),
                               (self.brightness_slider, "valueChanged"),
                               (self.speed_slider, "valueChanged"),
                               (self.sleep_combo, "currentIndexChanged"),
                               (self.deep_spin, "valueChanged"),
                               (self.debounce_combo, "currentIndexChanged")):
            self._watch(widget, signal)
        # Picking one of the two DPI modes makes the swatch above stop applying,
        # and the tab should say so as soon as it is picked.
        self.mode_combo.currentIndexChanged.connect(
            lambda *_: self._say(self._mode_note()))
        self.color_button.colorChanged.connect(lambda *_: self.changed.emit())

    def _mode_note(self) -> str:
        """Say so when the chosen mode ignores the colour above it."""
        if self.mode_combo.currentText() not in p.DPI_LIGHT_MODES:
            return ""
        return ("in this mode the LED is lit in the active DPI stage's colour, so "
                "the colour above does not apply")

    def _widgets(self) -> tuple:
        return (self.mode_combo, self.sleep_combo, self.debounce_combo,
                self.brightness_slider, self.speed_slider, self.deep_spin,
                self.color_button)

    def load(self, state: dict) -> None:
        settings = state.get("lighting")
        if settings is None:
            self._absent("lighting")
            return
        self.setEnabled(True)
        self._say("")
        with _quiet(*self._widgets()):
            added = _select(self.mode_combo, int(settings.mode), settings.mode_name)
            self.color_button.set_rgb(settings.rgb)
            self.brightness_slider.setValue(
                max(p.BRIGHTNESS_MIN, min(int(settings.brightness), p.BRIGHTNESS_MAX)))
            self.speed_slider.setValue(
                max(p.LED_SPEED_MIN, min(int(settings.led_speed), p.LED_SPEED_MAX)))
            _select(self.sleep_combo, float(settings.sleep), lambda value: f"{value:g} min")
            self.deep_spin.setValue(int(settings.deep_sleep))
            _select(self.debounce_combo, int(settings.debounce),
                    lambda value: f"{value} ms")
            if added:
                self._say(f"the mouse reports light mode {settings.mode:#04x}, which "
                          f"the vendor's writer does not define; it is kept as-is "
                          f"unless you pick another one")
            else:
                self._say(self._mode_note())

    def collect(self, state: dict) -> list:
        settings = state["lighting"]
        settings.mode = int(self.mode_combo.currentData())
        settings.rgb = self.color_button.rgb()
        settings.brightness = int(self.brightness_slider.value())
        settings.led_speed = int(self.speed_slider.value())
        settings.sleep = float(self.sleep_combo.currentData())
        settings.deep_sleep = int(self.deep_spin.value())
        settings.debounce = int(self.debounce_combo.currentData())
        return list(self.keys)


class ButtonsPanel(SettingsPanel):
    """Report 0x08: the 18 button slots, edited as the CLI's binding specs.

    The spec strings (``disabled``, ``key:shift+a``, ``macro:2``) come from
    :func:`protocol.parse_action_spec`, so a binding typed here can be pasted
    into ``atksoft set buttons --set 4=key:shift+a`` and vice versa.

    A slot whose action byte the firmware table does not know is left alone
    rather than blanked: the text form would lose it, and losing a binding to a
    round-trip through the GUI is worse than not being able to edit it.
    """

    keys = ("buttons",)

    def build(self) -> None:
        table = QTableWidget(p.BUTTON_SLOTS, 3, self)
        table.setHorizontalHeaderLabels(["Button", "Binding", ""])
        table.verticalHeader().setVisible(False)
        table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.edits: list = []
        self.pickers: list = []
        self._locked: list = []
        for index in range(p.BUTTON_SLOTS):
            number = index + 1
            item = QTableWidgetItem(p.BUTTON_NAMES.get(number, f"Slot {number}"))
            item.setFlags(Qt.ItemFlag.ItemIsEnabled)
            table.setItem(index, 0, item)
            edit = QLineEdit(table)
            edit.setPlaceholderText("disabled, fire, key:shift+a, macro:2 …")
            edit.textChanged.connect(lambda *_: self.changed.emit())
            edit.textChanged.connect(lambda _text, row=index: self._validate_row(row))
            if number in p.READ_ONLY_SLOTS:
                edit.setEnabled(False)
                edit.setToolTip("read-only in the vendor tool as well -- the firmware "
                                "ignores a write to this slot")
            table.setCellWidget(index, 1, edit)
            picker = QPushButton("Key…", table)
            picker.setFixedWidth(64)
            picker.clicked.connect(lambda *_, row=index: self._pick_key(row))
            table.setCellWidget(index, 2, picker)
            self.edits.append(edit)
            self.pickers.append(picker)
            self._locked.append(False)
        self.table = table
        self._layout.addWidget(table)
        hint = QLabel("Binding syntax: an action name (disabled, left-click, fire, "
                      "dpi-cycle, …), key:<name>[+ctrl+shift+alt+win] for a keyboard "
                      "key, or macro:<id> -- the same strings as "
                      "'atksoft set buttons --set'.", self)
        hint.setWordWrap(True)
        self._layout.addWidget(hint)
        self.reset_button = QPushButton("Reset to factory mapping", self)
        self.reset_button.setToolTip("fills the table with the vendor's own default "
                                     "binding; nothing is written until you save")
        self.reset_button.clicked.connect(self._reset)
        self._layout.addWidget(self.reset_button)

    # -- validation --------------------------------------------------------

    def _validate_row(self, row: int) -> bool:
        """Paint a row red while its text does not parse."""
        if self._locked[row]:
            return True
        text = self.edits[row].text().strip()
        try:
            p.parse_action_spec(text)
        except (p.ProtocolError, ValueError) as error:
            self.edits[row].setStyleSheet("background-color: #7a3b3b;")
            self.edits[row].setToolTip(str(error))
            return False
        self.edits[row].setStyleSheet("")
        self.edits[row].setToolTip("")
        return True

    def problems(self) -> list:
        """One message per slot whose text does not parse."""
        return [f"button slot {row + 1}: {self.edits[row].toolTip()}"
                for row in range(p.BUTTON_SLOTS) if not self._validate_row(row)]

    def _reset(self) -> None:
        defaults = p.ButtonSettings.default().slots
        with _quiet(*self.edits):
            for index, slot in enumerate(defaults):
                if not self._locked[index]:
                    self.edits[index].setText(p.format_action_spec(slot))
        self.changed.emit()

    def _pick_key(self, row: int) -> None:
        """Ask for a key by name and keep whatever modifiers the row had."""
        dialog = KeyPickerDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        modifiers = 0
        try:
            modifiers = p.parse_action_spec(self.edits[row].text().strip()).modifiers
        except (p.ProtocolError, ValueError):
            modifiers = 0
        spec = p.format_action_spec(p.ButtonSlot(
            action=p.ACTION_BY_NAME["keyboard-key"], modifiers=modifiers,
            usage=dialog.usage()))
        self.edits[row].setText(spec)


    # -- load / collect ----------------------------------------------------

    def load(self, state: dict) -> None:
        settings = state.get("buttons")
        if settings is None:
            self._absent("buttons")
            return
        self.setEnabled(True)
        self._say("")
        with _quiet(*self.edits):
            for index in range(p.BUTTON_SLOTS):
                slot = settings.slot(index + 1)
                known = slot.action in p.FIRMWARE_ACTIONS
                self._locked[index] = not known
                editable = known and (index + 1) not in p.READ_ONLY_SLOTS
                self.edits[index].setEnabled(editable)
                self.pickers[index].setEnabled(editable)
                if known:
                    self.edits[index].setText(p.format_action_spec(slot))
                else:
                    self.edits[index].setText("")
                    self.edits[index].setPlaceholderText(
                        f"unknown action {slot.action:#04x} -- kept as it was read")
        for row in range(p.BUTTON_SLOTS):
            self._validate_row(row)

    def collect(self, state: dict) -> list:
        problems = self.problems()
        if problems:
            raise p.ProtocolError("; ".join(problems))
        settings = state["buttons"]
        for index in range(p.BUTTON_SLOTS):
            if self._locked[index]:
                continue
            # Straight into the list rather than through ``set_slot``: the
            # read-only rule is already enforced by the disabled row, and the
            # deep copy being edited here is thrown away if a write is rejected.
            settings.slots[index] = p.parse_action_spec(self.edits[index].text().strip())
        return list(self.keys)


class KeyPickerDialog(QDialog):
    """Pick a HID key by name, for the button table's *Key…* buttons.

    Names come from :mod:`atksoft.keynames`, the same table ``key:`` specs are
    parsed with, so anything pickable here is typeable there.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Pick a key")
        self.resize(340, 460)
        layout = QVBoxLayout(self)
        self.search = QLineEdit(self)
        self.search.setPlaceholderText("type to filter (name or code)…")
        self.list = QListWidget(self)
        for code, name in sorted({**keynames.KEY_NAMES,
                                  **keynames.MOUSE_CODES}.items()):
            item = QListWidgetItem(f"{name}   ({code:#04x})")
            item.setData(Qt.ItemDataRole.UserRole, code)
            self.list.addItem(item)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            self)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(self.search)
        layout.addWidget(self.list)
        layout.addWidget(buttons)
        self.search.textChanged.connect(self._filter)
        self.list.itemDoubleClicked.connect(lambda *_: self.accept())
        if self.list.count():
            self.list.setCurrentRow(0)

    def _filter(self, text: str) -> None:
        needle = str(text).strip().lower()
        for row in range(self.list.count()):
            item = self.list.item(row)
            item.setHidden(bool(needle) and needle not in item.text().lower())
        for row in range(self.list.count()):
            if not self.list.item(row).isHidden():
                self.list.setCurrentRow(row)
                return

    def usage(self) -> int:
        """The HID usage id of the highlighted key (0 when nothing is picked)."""
        item = self.list.currentItem()
        return int(item.data(Qt.ItemDataRole.UserRole)) if item is not None else 0


class DevicePanel(SettingsPanel):
    """Whole-mouse settings (live profile, profile count, polling) plus facts.

    The read-only half is the same information ``atksoft list`` prints, because
    "the button report is not exposed" and "the mouse is asleep" otherwise look
    like the same failure from a tab that will not fill in.
    """

    keys = ("profile", "polling")

    def build(self) -> None:
        group = QGroupBox("Profiles and polling", self)
        form = QFormLayout(group)
        self.current_spin = QSpinBox(group)
        self.current_spin.setRange(1, p.MAX_PROFILES)
        self.current_spin.setPrefix("live profile ")
        self.current_spin.setToolTip("which on-board profile the mouse is using")
        self.maximum_spin = QSpinBox(group)
        self.maximum_spin.setRange(1, p.MAX_PROFILES)
        self.maximum_spin.setPrefix("count ")
        self.maximum_spin.setToolTip("how many profiles the mouse exposes")
        self.polling_combo = QComboBox(group)
        for hz in p.POLL_RATES:
            self.polling_combo.addItem(f"{hz} Hz", hz)
        form.addRow(self.current_spin)
        form.addRow(self.maximum_spin)
        form.addRow("polling rate", self.polling_combo)
        self._watch(self.current_spin, "valueChanged")
        self._watch(self.maximum_spin, "valueChanged")
        self._watch(self.polling_combo, "currentIndexChanged")
        self._layout.addWidget(group)

        facts = QGroupBox("This link", self)
        box = QVBoxLayout(facts)
        self.device_label = QLabel("not connected", facts)
        self.device_label.setWordWrap(True)
        self.battery_label = QLabel("battery: not read yet", facts)
        self.battery_button = QPushButton("Read battery", facts)
        self.battery_button.setToolTip("battery events only arrive while the mouse "
                                       "is on its dongle and awake; give it a click "
                                       "or two and try again")
        box.addWidget(self.device_label)
        box.addWidget(self.battery_label)
        box.addWidget(self.battery_button)
        self.descriptor_table = QTableWidget(0, 4, facts)
        self.descriptor_table.setHorizontalHeaderLabels(
            ["report", "what", "wire bytes", "declared read"])
        self.descriptor_table.verticalHeader().setVisible(False)
        self.descriptor_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers)
        self.descriptor_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch)
        self.descriptor_table.setMaximumHeight(240)
        box.addWidget(self.descriptor_table)
        self._layout.addWidget(facts)

    # -- facts -------------------------------------------------------------

    def show_capabilities(self, caps: dict) -> None:
        """Fill in the node facts and the descriptor inventory."""
        if not caps:
            self.device_label.setText("not connected")
            self.descriptor_table.setRowCount(0)
            return
        self.device_label.setText(
            f"{caps.get('name') or 'unknown device'} -- "
            f"{p.VENDOR_ID:04x}:{caps.get('pid', 0):04x}, interface "
            f"{caps.get('interface')}, {caps.get('path')}")
        features = caps.get("features", {})
        self.descriptor_table.setRowCount(len(features))
        for row, (rid, payload) in enumerate(sorted(features.items())):
            name = REPORT_NAMES.get(rid, f"report {rid:#04x}")
            declared = p.READ_GRANT_LENGTH.get(rid)
            cells = (f"{rid:#04x}", name, str(payload + 1),
                     "—" if declared is None else str(declared))
            for column, text in enumerate(cells):
                self.descriptor_table.setItem(row, column, QTableWidgetItem(text))

    def show_battery(self, event) -> None:
        """Render a battery event (or say why there is nothing to render)."""
        if event is None:
            self.battery_label.setText(
                "battery: no event -- the link may be the cable, or the mouse asleep")
            return
        state = event.state_name
        self.battery_label.setText(f"battery: {event.percent}% ({state})")

    # -- settings ----------------------------------------------------------

    def load(self, state: dict) -> None:
        wanted = [key for key in self.keys if key in state]
        if not wanted:
            self._absent(self.keys[0])
            return
        self.setEnabled(True)
        self._say("" if len(wanted) == len(self.keys) else
                  f"this link exposes only {', '.join(wanted)} of "
                  f"{', '.join(self.keys)}")
        profiles = state.get("profile")
        with _quiet(self.current_spin, self.maximum_spin, self.polling_combo):
            if profiles is not None:
                self.current_spin.setValue(int(profiles.current))
                self.maximum_spin.setValue(int(profiles.maximum))
            polling = state.get("polling")
            if polling is not None:
                _select(self.polling_combo, int(polling.hz), lambda hz: f"{hz} Hz")

    def collect(self, state: dict) -> list:
        keys = []
        profiles = state.get("profile")
        if profiles is not None:
            profiles.current = int(self.current_spin.value())
            profiles.maximum = int(self.maximum_spin.value())
            keys.append("profile")
        polling = state.get("polling")
        if polling is not None:
            polling.hz = int(self.polling_combo.currentData())
            keys.append("polling")
        return keys


class AdvancedPanel(QWidget):
    """Raw feature reports: the escape hatch, with the warning it deserves.

    Everything else in this window goes through ``session.apply``; this tab
    sends the bytes in the text box.  It exists because the reports the codecs
    do not understand yet (0x07, 0x09, 0x0a, and whatever a different revision
    adds) are still readable and still worth poking at -- not because writing
    arbitrary frames to a mouse's firmware is a good idea.
    """

    #: report id, declared length (None = the vendor's own), parameter
    readRequested = pyqtSignal(int, object, object)
    #: the frame to send
    writeRequested = pyqtSignal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        warning = QLabel(
            "<b>Raw reports.</b> A frame written here reaches the firmware exactly "
            "as typed: no read-modify-write, no checksum repair, nothing to undo it. "
            "Read a report first and edit that frame if you do not know what a field "
            "does -- the codecs cannot parse a frame that never had a valid "
            "checksum.", self)
        warning.setWordWrap(True)
        layout.addWidget(warning)

        read = QGroupBox("Read one report", self)
        row = QHBoxLayout(read)
        self.rid_edit = QLineEdit("04", read)
        self.rid_edit.setFixedWidth(60)
        self.rid_edit.setToolTip("feature report id in hex, e.g. 04 or 0x0a")
        self.declared_spin = QSpinBox(read)
        self.declared_spin.setRange(0, 0xFF)
        self.declared_spin.setPrefix("declared ")
        self.declared_spin.setSpecialValueText("declared auto")
        self.declared_spin.setToolTip("the length declared in the 0xA0 read request; "
                                      "0 uses the vendor table's value for this report")
        self.parameter_spin = QSpinBox(read)
        self.parameter_spin.setRange(0, 0xFF)
        self.parameter_spin.setPrefix("param ")
        self.parameter_box = QCheckBox("override", read)
        self.parameter_box.setToolTip("byte 4 of the 0xA0 request: the profile id for "
                                      "0x04/0x08, the macro id for 0x09")
        self.read_button = QPushButton("Read", read)
        for widget in (self.rid_edit, self.declared_spin, self.parameter_spin,
                       self.parameter_box, self.read_button):
            row.addWidget(widget)
        row.addStretch(1)
        layout.addWidget(read)

        quick = QGroupBox("Reports nobody has decoded yet", self)
        qrow = QHBoxLayout(quick)
        self.quick_buttons = []
        for rid, label in RAW_BUTTONS:
            button = QPushButton(label, quick)
            button.clicked.connect(lambda *_, report=rid: self.readRequested.emit(
                report, None, None))
            qrow.addWidget(button)
            self.quick_buttons.append(button)
        qrow.addStretch(1)
        layout.addWidget(quick)

        write = QGroupBox("Write one frame", self)
        wbox = QVBoxLayout(write)
        self.write_edit = QPlainTextEdit(write)
        _mono(self.write_edit)
        self.write_edit.setPlaceholderText("06 09 01 01 fe 00 00 00 00")
        self.write_edit.setMaximumHeight(80)
        wrow = QHBoxLayout()
        self.fill_button = QPushButton("Fill from the last read", write)
        self.fill_button.setToolTip("copy the frame on screen above, so a no-op "
                                    "write can be checked end to end")
        self.understand_box = QCheckBox(
            "I understand this skips the read-modify-write path", write)
        self.write_button = QPushButton("Write frame", write)
        self.write_button.setEnabled(False)
        wrow.addWidget(self.fill_button)
        wrow.addWidget(self.understand_box, 1)
        wrow.addWidget(self.write_button)
        wbox.addWidget(self.write_edit)
        wbox.addLayout(wrow)
        layout.addWidget(write)

        self.output = QPlainTextEdit(self)
        _mono(self.output)
        self.output.setReadOnly(True)
        self.output.setMinimumHeight(160)
        layout.addWidget(self.output, 1)
        self.copy_button = QPushButton("Copy the last frame", self)
        layout.addWidget(self.copy_button)

        self.read_button.clicked.connect(self._do_read)
        self.write_button.clicked.connect(self._do_write)
        self.fill_button.clicked.connect(self._do_fill)
        self.copy_button.clicked.connect(self._do_copy)
        self._busy = False
        self._last = b""
        self.understand_box.toggled.connect(self._sync_write)


    # -- actions -----------------------------------------------------------

    def _do_read(self) -> None:
        try:
            rid = parse_report_id(self.rid_edit.text()) & 0xFF
        except p.ProtocolError as error:
            self.say(f"cannot read: {error}")
            return
        declared = int(self.declared_spin.value()) or None
        parameter = (int(self.parameter_spin.value())
                     if self.parameter_box.isChecked() else None)
        self.readRequested.emit(rid, declared, parameter)

    def _do_write(self) -> None:
        try:
            frame = parse_hex(self.write_edit.toPlainText().strip())
        except p.ProtocolError as error:
            self.say(f"cannot write: {error}")
            return
        if not frame:
            self.say("cannot write: the frame is empty")
            return
        self.writeRequested.emit(frame)

    def _do_fill(self) -> None:
        if self._last:
            self.write_edit.setPlainText(" ".join(f"{byte:02x}" for byte in self._last))

    def _do_copy(self) -> None:
        if not self._last:
            self.say("nothing has been read yet")
            return
        QApplication.clipboard().setText(" ".join(f"{byte:02x}" for byte in self._last))
        self.say("copied the last frame to the clipboard")

    # -- display -----------------------------------------------------------

    def say(self, text: str) -> None:
        self.output.appendPlainText(text)

    def show_frame(self, rid: int, frame, moved=None) -> None:
        self._last = bytes(frame)
        self.rid_edit.setText(f"{rid:02x}")
        lines = [f"read {rid:#04x}: {len(self._last)} bytes"
                 + (f" (the kernel moved {moved})" if moved else ""),
                 hexdump(self._last)]
        printable = "".join(chr(byte) if 0x20 <= byte < 0x7F else "."
                            for byte in self._last[2:])
        if any(char != "." for char in printable):
            lines.append(f"payload as text: {printable}")
        self.say("\n".join(lines))

    def show_written(self, frame) -> None:
        self.say(f"wrote {bytes(frame)[0]:#04x}: {len(frame)} bytes\n"
                 f"{hexdump(frame)}")

    def _sync_write(self) -> None:
        self.write_button.setEnabled(not self._busy
                                     and self.understand_box.isChecked())

    def set_busy(self, busy: bool) -> None:
        self._busy = bool(busy)
        self.read_button.setEnabled(not busy)
        for button in self.quick_buttons:
            button.setEnabled(not busy)
        self._sync_write()


class MonitorPanel(QWidget):
    """Streams interrupt-IN events and polls the battery.

    Events are the only part of the protocol that arrives unasked: press the
    DPI button or switch profiles while watching and the mouse says so here,
    which is the cheapest way to tell a live link from a silent one.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        row = QHBoxLayout()
        self.watch_button = QPushButton("Start watching", self)
        self.watch_button.setCheckable(True)
        self.battery_button = QPushButton("Read battery", self)
        self.count_label = QLabel("0 events", self)
        self.clear_button = QPushButton("Clear", self)
        for widget in (self.watch_button, self.battery_button, self.count_label,
                       self.clear_button):
            row.addWidget(widget)
        row.addStretch(1)
        layout.addLayout(row)
        hint = QLabel("Click the mouse's buttons or press its DPI button while "
                      "watching: profile changes, DPI stages, battery levels and the "
                      "firmware's answer to every write arrive here.  Events are the "
                      "one thing the codecs cannot ask for.", self)
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.log = QPlainTextEdit(self)
        _mono(self.log)
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(4000)
        layout.addWidget(self.log, 1)
        self.clear_button.clicked.connect(self.clear)
        self._count = 0

    def append(self, text: str) -> None:
        stamp = datetime.datetime.now().strftime("%H:%M:%S")
        self.log.appendPlainText(f"{stamp}  {text}")

    def note_event(self, event: dict) -> None:
        self._count += 1
        self.count_label.setText(f"{self._count} event{'s' if self._count != 1 else ''}")
        self.append(_format_event(event))

    def clear(self) -> None:
        self.log.clear()
        self._count = 0
        self.count_label.setText("0 events")

    def set_watching(self, watching: bool) -> None:
        self.watch_button.setChecked(bool(watching))
        self.watch_button.setText("Stop watching" if watching else "Start watching")
        self.battery_button.setEnabled(not watching)


def _format_event(event: dict) -> str:
    """One line per event, whatever the event turned out to be."""
    kind = event.get("event")
    if kind == "battery":
        return (f"battery: {event.get('percent')}% "
                f"({event.get('state_name')})"
                + (" -- on the cable" if event.get("charging") else ""))
    if kind == "dpi-cycle":
        return f"dpi stage -> {event.get('stage')}"
    if kind == "profile-changed":
        return f"profile -> {event.get('profile')}"
    if kind == "vibration-mode":
        return f"vibration mode -> {event.get('mode_name', event.get('mode'))}"
    if kind == "command-confirmation":
        verdict = "confirmed" if event.get("ok") else "REFUSED"
        return (f"firmware {verdict} a write to report "
                f"{event.get('report_id', 0):#04x}")
    return "unknown event: " + ", ".join(f"{key}={value}" for key, value in event.items())


# ============================================================== the window

class PanelPage(QWidget):
    """A settings panel plus the Save/Revert footer every settings tab shares.

    The footer is the contract with the user: nothing is written until *Save
    changes* is pressed, and it is only live while there are unsaved edits.
    """

    saveRequested = pyqtSignal()
    revertRequested = pyqtSignal()

    def __init__(self, panel: SettingsPanel, parent=None):
        super().__init__(parent)
        self.panel = panel
        self.keys = tuple(panel.keys)
        self._dirty = False
        self._busy = False
        self._available: set = set()
        layout = QVBoxLayout(self)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setWidget(panel)
        layout.addWidget(scroll, 1)
        footer = QHBoxLayout()
        self.status = QLabel("nothing read yet", self)
        self.status.setWordWrap(True)
        footer.addWidget(self.status, 1)
        self.revert_button = QPushButton("Revert", self)
        self.revert_button.setToolTip("put the last values read from the mouse back "
                                      "into this tab; nothing is written")
        self.save_button = QPushButton("Save changes", self)
        footer.addWidget(self.revert_button)
        footer.addWidget(self.save_button)
        layout.addLayout(footer)
        panel.changed.connect(self.mark_dirty)
        self.save_button.clicked.connect(self.saveRequested.emit)
        self.revert_button.clicked.connect(self.revertRequested.emit)
        self._sync()

    def load(self, state: dict) -> None:
        """Show the device's own values again and call the tab clean."""
        self._available = {key for key in self.keys if key in state}
        self.panel.load(state)
        self._dirty = False
        self.status.setText("matches the mouse" if self._available
                            else "no editable report on this link")
        self._sync()

    def collect(self, state: dict) -> list:
        """Write the widgets into a copy of the last read; return the keys."""
        return self.panel.collect(state)

    def mark_dirty(self) -> None:
        if not self._available:
            return
        self._dirty = True
        self.status.setText("unsaved changes")
        self._sync()

    def mark_clean(self, message: str = "saved") -> None:
        self._dirty = False
        self.status.setText(message)
        self._sync()

    def show_problem(self, text: str) -> None:
        self._dirty = True
        self.status.setText(f"not written: {text}")
        self._sync()

    def set_busy(self, busy: bool) -> None:
        self._busy = bool(busy)
        self._sync()

    def _sync(self) -> None:
        live = bool(self._available) and not self._busy
        self.save_button.setEnabled(live and self._dirty)
        self.revert_button.setEnabled(live and self._dirty)


class MainWindow(QMainWindow):
    """The control panel: one tab per report, one worker for the device."""

    def __init__(self, link: DeviceLink, node: str = "", profile: int = 1, parent=None):
        super().__init__(parent)
        self.link = link
        self.node = node or ""
        self.state: dict = {}
        self.caps: dict = {}
        self.profile = max(1, int(profile))
        #: the message a save wants to leave on the status bar (see _on_loaded)
        self._pending_status = ""
        self.setWindowTitle("ATKsoft -- Attack Shark control panel")
        self.resize(940, 780)

        self.dpi_panel = DpiPanel(self)
        self.lighting_panel = LightingPanel(self)
        self.buttons_panel = ButtonsPanel(self)
        self.device_panel = DevicePanel(self)
        self.advanced_panel = AdvancedPanel(self)
        self.monitor_panel = MonitorPanel(self)
        self.pages = {
            "dpi": PanelPage(self.dpi_panel, self),
            "lighting": PanelPage(self.lighting_panel, self),
            "buttons": PanelPage(self.buttons_panel, self),
            "device": PanelPage(self.device_panel, self),
        }
        self.tabs = QTabWidget(self)
        for title, page in (("DPI", self.pages["dpi"]),
                            ("Lighting", self.pages["lighting"]),
                            ("Buttons", self.pages["buttons"]),
                            ("Device", self.pages["device"]),
                            ("Advanced", self.advanced_panel),
                            ("Monitor", self.monitor_panel)):
            self.tabs.addTab(page, title)
        self.setCentralWidget(self.tabs)
        for page in self.pages.values():
            page.saveRequested.connect(lambda page=page: self.save_page(page))
            page.revertRequested.connect(lambda page=page: self.revert_page(page))

        self._build_toolbar()
        self._build_status()
        self._wire_device()

    # -- construction ------------------------------------------------------

    def _build_toolbar(self) -> None:
        bar = QToolBar("Device", self)
        bar.setMovable(False)
        self.addToolBar(bar)
        bar.addWidget(QLabel(" node ", self))
        self.node_combo = QComboBox(self)
        self.node_combo.setMinimumWidth(340)
        bar.addWidget(self.node_combo)
        self.connect_button = QPushButton("Connect", self)
        self.connect_button.clicked.connect(self.connect_to_device)
        bar.addWidget(self.connect_button)
        bar.addSeparator()
        bar.addWidget(QLabel(" edit profile ", self))
        self.profile_spin = QSpinBox(self)
        self.profile_spin.setRange(1, p.MAX_PROFILES)
        self.profile_spin.setValue(self.profile)
        self.profile_spin.setToolTip("which profile the DPI and button tabs read and "
                                     "write; the live profile is set on the Device tab")
        bar.addWidget(self.profile_spin)
        self.reload_button = QPushButton("Reload", self)
        self.reload_button.setToolTip("read every report again and throw away "
                                      "unsaved edits")
        self.reload_button.clicked.connect(self.reload)
        bar.addWidget(self.reload_button)
        bar.addSeparator()
        self.busy_label = QLabel("", self)
        bar.addWidget(self.busy_label)
        self._fill_nodes()

    def _build_status(self) -> None:
        self.node_label = QLabel("no device", self)
        self.battery_label = QLabel("", self)
        self.statusBar().addPermanentWidget(self.node_label)
        self.statusBar().addPermanentWidget(self.battery_label)
        self.statusBar().showMessage("pick a node and press Connect")

    def _wire_device(self) -> None:
        """All the signal plumbing in one place."""
        self.link.opened.connect(self._on_opened)
        self.link.loaded.connect(self._on_loaded)
        self.link.applied.connect(self._on_applied)
        self.link.rawRead.connect(self._on_raw_read)
        self.link.wrote.connect(self._on_wrote)
        self.link.failed.connect(self._on_failed)
        self.link.busyChanged.connect(self._set_busy)
        self.link.eventFired.connect(self._on_event)
        self.link.batteryRead.connect(self._on_battery)
        self.link.watchState.connect(self.monitor_panel.set_watching)
        self.advanced_panel.readRequested.connect(self.link.read_raw)
        self.advanced_panel.writeRequested.connect(self.link.write_raw)
        self.monitor_panel.battery_button.clicked.connect(self.link.battery)
        self.device_panel.battery_button.clicked.connect(self.link.battery)
        self.monitor_panel.watch_button.clicked.connect(self.toggle_watch)

    def _fill_nodes(self, prefer: str = "") -> None:
        """List the mouse's hidraw nodes, config interface first (see session).

        ``prefer`` keeps the current selection across a refresh: the node index
        changes on every replug, so re-listing is the only way to pick up a
        dongle that was plugged in after the window opened.
        """
        self.node_combo.clear()
        self.node_combo.addItem("auto: find the mouse", "")
        try:
            nodes = list_nodes()
        except OSError as error:
            nodes = []
            self._notify(f"cannot enumerate hidraw nodes: {error}")
        for node in nodes:
            kind = "config" if is_config_node(node) else "input"
            self.node_combo.addItem(f"{node.path}  {kind}  {node.name}", node.path)
        position = self.node_combo.findData(prefer or self.node)
        if position >= 0:
            self.node_combo.setCurrentIndex(position)
        if len(self.node_combo) == 1:
            self._notify("no Attack Shark node found -- plug in the dongle (and check "
                         "'atksoft list')")


    # -- actions -----------------------------------------------------------

    def connect_to_device(self) -> None:
        """Open the node selected in the toolbar, after re-listing the nodes.

        Re-listing matters: ``/dev/hidrawN`` is not stable across replugs, and a
        dongle plugged in after the window opened is not in the list yet.  A
        pinned ``--node`` outranks the list even when it is not in it: quietly
        opening a *different* mouse than the one asked for is worse than the
        error ``session`` raises.
        """
        wanted = self.node_combo.currentData() or self.node
        self._fill_nodes(prefer=wanted)
        self.link.open(wanted or "")

    def reload(self) -> None:
        self.link.reload(self.profile_spin.value())

    def toggle_watch(self) -> None:
        if self.monitor_panel.watch_button.isChecked():
            self.monitor_panel.set_watching(True)
            self.monitor_panel.append("watching for events…")
            self.link.watch()
        else:
            self.link.stop_watch()

    def save_page(self, page: PanelPage) -> None:
        """Collect one tab's edits and hand them to the worker.

        The edits go into a deep copy of the last read: if the write is refused
        (a refused grant, an unplugged dongle) the window still holds the
        device's own values, so *Revert* means revert.
        """
        working = {key: copy.deepcopy(value) for key, value in self.state.items()}
        try:
            keys = [key for key in page.collect(working) if key in working]
        except (p.ProtocolError, ValueError, KeyError) as error:
            page.show_problem(str(error))
            self._notify(f"not written: {error}")
            return
        if not keys:
            page.mark_clean("nothing to write on this link")
            return
        self.link.apply({key: working[key] for key in keys}, keys)

    def revert_page(self, page: PanelPage) -> None:
        page.load(self.state)
        page.mark_clean("reverted to what the mouse reported")

    def _on_opened(self, path: str, caps: dict) -> None:
        self.caps = dict(caps or {})
        self.device_panel.show_capabilities(self.caps)
        self.node_label.setText(f"{path}  {p.VENDOR_ID:04x}:{self.caps.get('pid', 0):04x}"
                                f"  if{self.caps.get('interface')}")
        self.monitor_panel.append(
            "opened " + path + ": "
            + (", ".join(f"{rid:02x}" for rid in sorted(self.caps.get("features", {})))
               or "no feature reports"))
        self.statusBar().showMessage(f"opened {path} -- reading the settings…")
        self.link.reload(self.profile_spin.value())

    def _on_loaded(self, state: dict, caps: dict) -> None:
        self.state = dict(state or {})
        if caps:
            self.caps = dict(caps)
            self.device_panel.show_capabilities(self.caps)
        for page in self.pages.values():
            page.load(self.state)
        # A save's own message outlives the read-back that follows it: _on_applied
        # runs first and leaves the summary here.
        message = self._pending_status or (
            "read " + (", ".join(sorted(self.state)) if self.state
                       else "nothing -- no settings report answered"))
        self._pending_status = ""
        self.statusBar().showMessage(message)


    def _on_applied(self, payload: dict) -> None:
        keys = list(payload.get("keys") or [])
        written = list(payload.get("written") or [])
        reports = dict(MouseSession.SETTINGS_REPORTS)
        for key in keys:
            frame = payload.get("frames", {}).get(key, b"")
            rid = reports.get(key)
            if rid in written:
                self.monitor_panel.append(f"wrote {key} (report {rid:#04x}, "
                                          f"{len(frame)} bytes)")
                self.monitor_panel.append("    " + hexdump(frame).replace("\n", "\n    "))
        if not written:
            self.statusBar().showMessage("nothing changed -- no frame was sent")
            for page in self.pages.values():
                page.mark_clean("already matches the mouse")
            return
        self._pending_status = (f"saved {'+'.join(keys)} -- read back from the mouse")
        for page in self.pages.values():
            page.mark_clean("saved")

    def _on_raw_read(self, rid: int, frame, moved) -> None:
        self.advanced_panel.show_frame(rid, frame, moved)
        self.statusBar().showMessage(f"read report {rid:#04x}: {len(frame)} bytes")

    def _on_wrote(self, frame) -> None:
        self.advanced_panel.show_written(frame)
        self.monitor_panel.append(f"wrote a raw frame to report {bytes(frame)[0]:#04x} "
                                  f"({len(frame)} bytes)")
        self.statusBar().showMessage(f"wrote {len(frame)} bytes to report "
                                     f"{bytes(frame)[0]:#04x}")

    def _on_failed(self, message: str) -> None:
        self._pending_status = ""
        self.monitor_panel.append(f"error: {message}")
        self.statusBar().showMessage(message)
        self.busy_label.setText("")

    def _on_event(self, event: dict) -> None:
        self.monitor_panel.note_event(event)
        if event.get("event") == "battery":
            self.battery_label.setText(f"battery {event.get('percent')}%")
        if event.get("event") == "dpi-cycle":
            self._follow_dpi_button(event.get("stage"))
        if event.get("event") == "profile-changed":
            # A physical button moved the live profile out from under the tabs.
            self.statusBar().showMessage(
                f"the mouse switched to profile {event.get('profile')} -- reload to "
                f"see it")

    def _follow_dpi_button(self, stage) -> None:
        """Move the DPI tab's *Current* radio to the stage the mouse landed on.

        The mouse's DPI button moves the stage without the app ever being asked,
        so the radio has to follow it or the tab goes on describing a stage the
        mouse is no longer on.  Only the radio moves: the mouse is already there,
        so nothing is written and the tab still says "matches the mouse".
        """
        try:
            stage = int(stage)
        except (TypeError, ValueError):
            return
        if not self.dpi_panel.show_current_stage(stage):
            return
        settings = self.state.get("dpi")
        if settings is not None:
            settings.current_stage = stage
        self.statusBar().showMessage(
            f"the mouse's DPI button chose stage {stage} -- the DPI tab is showing "
            f"it, nothing was written")

    def _on_battery(self, event) -> None:
        self.device_panel.show_battery(event)
        if event is None:
            self.battery_label.setText("")
            self.statusBar().showMessage(
                "no battery event -- the link may be the cable, or the mouse asleep")
            return
        self.battery_label.setText(f"battery {event.percent}%")
        self.statusBar().showMessage(f"battery: {event.percent}% ({event.state_name})")

    def _set_busy(self, busy: bool, what: str = "") -> None:
        self.busy_label.setText(what if busy else "")
        self.connect_button.setEnabled(not busy)
        self.reload_button.setEnabled(not busy)
        self.profile_spin.setEnabled(not busy)
        self.advanced_panel.set_busy(busy)
        for page in self.pages.values():
            page.set_busy(busy)

    def _notify(self, text: str) -> None:
        self.monitor_panel.append(text)
        self.statusBar().showMessage(text)

    def closeEvent(self, event) -> None:                # noqa: N802 - Qt's name
        self.link.stop()
        super().closeEvent(event)


# ============================================================ command line

def _parser() -> argparse.ArgumentParser:
    """``atksoft-gui`` / ``atksoft gui`` / ``python -m atksoft.gui`` options."""
    parser = argparse.ArgumentParser(
        prog="atksoft-gui",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="The PyQt6 control panel for an Attack Shark mouse.",
        epilog="""\
The window reads every report the dongle exposes when it opens, shows each one
on its own tab and writes nothing until "Save changes" is pressed.  Saving is
the same read-modify-write the CLI does: the report is fetched, edited in memory
and sent back whole, so one field cannot disturb the rest of the frame.

Every write is read back afterwards, so the tabs show what the mouse stored
rather than what was asked for.

examples:
  atksoft gui                      # find the mouse and connect
  atksoft gui --node /dev/hidraw2   # pin the node instead of searching
  atksoft gui --profile 2           # edit profile 2's DPI and buttons
  atksoft gui --no-connect          # open the window, connect by hand
""")
    parser.add_argument("--node", metavar="PATH",
                        help="use this /dev/hidrawN instead of searching for the mouse")
    parser.add_argument("--profile", type=int, default=1, metavar="N",
                        help="profile the DPI and button tabs edit, 1..%d "
                             "(default: 1)" % p.MAX_PROFILES)
    parser.add_argument("--no-connect", action="store_true",
                        help="do not touch the mouse until Connect is pressed")
    return parser


def main(argv=None) -> int:
    """Entry point for ``atksoft gui``, ``atksoft-gui`` and ``-m atksoft.gui``."""
    args = _parser().parse_args(sys.argv[1:] if argv is None else argv)
    app = QApplication.instance() or QApplication(["atksoft-gui"])
    link = DeviceLink()
    window = MainWindow(link, node=args.node or "", profile=args.profile)
    window.show()
    if not args.no_connect:
        window.connect_to_device()
    try:
        return app.exec()
    finally:
        link.stop()


if __name__ == "__main__":                              # pragma: no cover
    sys.exit(main())
