"""``atksoft`` -- command line control for the Attack Shark X6 mouse.

Examples::

    atksoft list                        # which hidraw node is the mouse
    atksoft read                        # everything the dongle will tell us
    atksoft set dpi --stage 1=800       # change one stage, keep the rest as read
    atksoft set lighting --mode neon --rgb ff00aa
    atksoft battery
    atksoft watch                       # stream events while you click things
    atksoft raw 0x06                    # one feature report, verbatim
    sudo atksoft install-udev           # stop having to use sudo

The layers, top to bottom::

    cli.py  ->  session.MouseSession  ->  hidraw.HidrawDevice  ->  /dev/hidrawN
                protocol.* (frame <-> dataclass codecs)

Nothing here touches the kernel itself, and every write is a read-modify-write:
the firmware hands out exactly one read per 0xA0 grant, so a setting is fetched,
edited in memory and sent back as a whole frame.  ``session.apply`` skips the
write when nothing actually changed, which is what makes it safe to poke at one
field without disturbing the other fifty bytes of the report.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

from . import protocol as p
from .hidraw import enumerate_hidraw
from .session import (REPORT_NAMES, MouseSession, SessionError, is_config_node,
                      list_nodes, open_session)

#: The udev rule shipped with the source tree (``data/`` is not inside the
#: package, so this path is only right for a git checkout -- an installed copy
#: can point ``--rules`` at wherever the file ended up).
RULES_SOURCE = Path(__file__).resolve().parent.parent / "data" / "60-atksoft.rules"
RULES_DEST_DIR = Path("/etc/udev/rules.d")

#: ``atksoft read`` order.  ``macro`` is deliberately absent: this dongle
#: declares a 64-byte report 0x09 while the vendor reader wants 131 bytes, so
#: it can only fail here (see protocol.MACRO_WRITE_NOTES) -- ask for it by name.
READ_SECTIONS = ("profile", "dpi", "lighting", "polling", "buttons",
                 "version", "battery", "wake", "blob")
READ_CHOICES = READ_SECTIONS + ("all", "macro")

#: Sections backed by ``session.read_settings`` / ``session.apply``.
SETTINGS_SECTIONS = ("dpi", "lighting", "polling", "buttons", "profile")


# ------------------------------------------------------------------ helpers

def emit(args, text: str, data) -> None:
    """Print ``data`` as JSON, or ``text`` for humans, depending on ``--json``."""
    if args.json:
        json.dump(data, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        sys.stdout.write(text if text.endswith("\n") else text + "\n")


def hexdump(data, width: int = 16) -> str:
    """``hexdump -C``-ish output: offset, hex pairs, printable column."""
    raw = bytes(data)
    lines = []
    for offset in range(0, len(raw), width):
        chunk = raw[offset:offset + width]
        hex_part = " ".join(f"{byte:02x}" for byte in chunk)
        ascii_part = "".join(chr(byte) if 32 <= byte < 127 else "." for byte in chunk)
        lines.append(f"{offset:04x}  {hex_part:<{width * 3 - 1}}  |{ascii_part}|")
    return "\n".join(lines)


def parse_assignment(spec: str, what: str) -> tuple:
    """``"3=1600"`` -> ``(3, "1600")``; shared by stages, colours and buttons."""
    key, sep, value = str(spec).partition("=")
    if not sep or not value:
        raise p.ProtocolError(f"{what} {spec!r} must look like N=VALUE")
    try:
        number = int(key, 10)
    except ValueError:
        raise p.ProtocolError(f"{what} {spec!r} must start with a number") from None
    return number, value


def parse_stage_list(value: str) -> list:
    """``"1,3 5"`` -> ``[1, 3, 5]``."""
    try:
        return [int(part, 10) for part in str(value).replace(",", " ").split()]
    except ValueError:
        raise p.ProtocolError(
            f"stage list {value!r} must be numbers separated by commas") from None


def parse_rgb(value: str) -> tuple:
    """``ff00aa``, ``#ff00aa`` or ``255,0,170`` -> ``(255, 0, 170)``."""
    text = str(value).strip().lstrip("#")
    parts = text.split(",")
    try:
        if len(parts) == 1 and len(text) == 6:
            return tuple(int(text[i:i + 2], 16) for i in (0, 2, 4))
        if len(parts) == 3:
            return tuple(int(part, 0) for part in parts)
    except ValueError:
        pass
    raise p.ProtocolError(f"colour {value!r} is neither RRGGBB nor r,g,b")


def parse_report_id(text) -> int:
    """Report ids are always written in hex: ``04``, ``0x04`` and ``4`` agree."""
    try:
        return int(str(text), 16)
    except ValueError:
        raise p.ProtocolError(f"report id {text!r} is not hex") from None


def parse_hex(text) -> bytes:
    """``"04 05 06"``, ``"040506"`` and ``"0x04,0x05"`` -> the same bytes."""
    cleaned = str(text).replace(",", " ").replace("0x", " ").replace("0X", " ")
    try:
        return bytes.fromhex(cleaned)
    except ValueError:
        raise p.ProtocolError(f"{text!r} is not a hex byte string") from None


def report_label(rid: int) -> str:
    """``0x04`` -> ``dpi``, for log lines."""
    return REPORT_NAMES.get(rid, f"report {rid:#04x}").replace(" ", "-")


def format_color(rgb) -> str:
    return "#%02x%02x%02x" % tuple(rgb)


def format_profile(settings) -> str:
    return f"profile: {settings.current} of {settings.maximum}"


def format_dpi(settings) -> str:
    lines = [f"dpi (profile {settings.profile}):"]
    for stage in range(1, p.MAX_DPI_STAGES + 1):
        flags = []
        if stage == settings.current_stage:
            flags.append("active")
        if not settings.stage_enabled(stage):
            flags.append("off")
        note = f"  ({', '.join(flags)})" if flags else ""
        lines.append(f"  stage {stage}: {settings.stage_dpi(stage):>6} dpi"
                     f"  {format_color(settings.stage_color(stage))}{note}")
    lines.append(f"  lift-off distance: {settings.lift_off_distance}"
                 f"  angle snap: {'on' if settings.angle_snap else 'off'}"
                 f"  motion sync: {'on' if settings.motion_sync else 'off'}"
                 f"  ripple control: {'on' if settings.ripple_control else 'off'}")
    return "\n".join(lines)


def format_lighting(settings) -> str:
    return (f"lighting (profile {settings.profile}):\n"
            f"  mode: {settings.mode_name}  colour: {format_color(settings.rgb)}\n"
            f"  brightness: {settings.brightness}/{p.BRIGHTNESS_MAX}"
            f"  speed: {settings.led_speed}/{p.LED_SPEED_MAX}\n"
            f"  sleep after: {settings.sleep} min"
            f"  deep sleep: {settings.deep_sleep} min\n"
            f"  debounce: {settings.debounce} ms")


def format_polling(settings) -> str:
    return f"polling rate: {settings.hz} Hz (profile {settings.profile})"


def format_buttons(settings) -> str:
    lines = [f"buttons (profile {settings.profile}):"]
    for number in range(1, p.BUTTON_SLOTS + 1):
        name = p.BUTTON_NAMES.get(number, f"Slot {number}")
        note = "  [read-only in the vendor tool]" if number in p.READ_ONLY_SLOTS else ""
        lines.append(f"  {number:>2}. {name:<22} {settings.slot(number).describe()}{note}")
    return "\n".join(lines)


def format_raw(report) -> str:
    """Version / wake / profile-blob reads, which the vendor never documented."""
    return (f"report {report.report_id:#04x} ({report.length} bytes):\n"
            f"{hexdump(report.payload)}")


def format_macro(macro) -> str:
    return macro.describe()


def format_battery(event) -> str:
    return f"battery: {event.percent}% ({event.state_name})"


def format_event(event) -> str:
    """One greppable line per interrupt-IN event."""
    if isinstance(event, p.BatteryEvent):
        return f"battery              {event.percent}% ({event.state_name})"
    if isinstance(event, p.DpiCycleEvent):
        return f"dpi-cycle            stage {event.stage} selected"
    if isinstance(event, p.ProfileChangedEvent):
        return f"profile-changed      profile {event.profile}"
    if isinstance(event, p.CommandConfirmationEvent):
        return (f"write-confirmation   {report_label(event.report_id)} "
                f"({event.report_id:#04x}) {event.status}")
    if isinstance(event, p.VibrationModeEvent):
        return f"vibration-mode       {event.mode_name}"
    return (f"{event.code_name:<20} device={event.device_name} "
            f"params={list(event.params)}")


#: section -> formatter, shared by ``read`` and the post-write confirmation.
FORMATTERS = {
    "profile": format_profile,
    "dpi": format_dpi,
    "lighting": format_lighting,
    "polling": format_polling,
    "buttons": format_buttons,
    "version": format_raw,
    "wake": format_raw,
    "blob": format_raw,
    "macro": format_macro,
    "battery": format_battery,
}


# -------------------------------------------------------------------- list

def _node_entry(node) -> dict:
    """Everything ``list`` knows about one hidraw node."""
    return {
        "path": node.path,
        "vid": f"{node.vid:04x}",
        "pid": f"{node.pid:04x}",
        "name": node.name,
        "interface": node.interface,
        "config": is_config_node(node),
        "openable": os.access(node.path, os.R_OK | os.W_OK),
        "reports": {f"{rid:02x}": length + 1 for rid, length in sorted(node.features.items())},
    }


def cmd_list(args) -> int:
    """Show the mouse's hidraw nodes, which one takes configuration, and what
    each interface exposes."""
    nodes = enumerate_hidraw() if args.all else list_nodes()
    if not nodes:
        raise SessionError(
            "no Attack Shark hidraw node found -- plug the 2.4 GHz dongle in, or "
            "pass --all to see every hidraw node this machine has")

    entries = [_node_entry(node) for node in nodes]
    lines = []
    for entry in entries:
        interface = "-" if entry["interface"] is None else str(entry["interface"])
        lines.append(f"{entry['path']}  {entry['vid']}:{entry['pid']}  if{interface}  "
                     f"{'config' if entry['config'] else 'input '}  "
                     f"{'open' if entry['openable'] else 'needs root/udev'}  {entry['name']}")
        if entry["config"]:
            inventory = "  ".join(
                f"{rid}:{REPORT_NAMES.get(int(rid, 16), '?').replace(' ', '-')}:{size}"
                for rid, size in entry["reports"].items())
            lines.append(f"    reports: {inventory}")
            if not entry["openable"]:
                lines.append(f"    hint: sudo atksoft install-udev   (then replug the dongle)")
    emit(args, "\n".join(lines), {"nodes": entries,
                                  "config": [e["path"] for e in entries if e["config"]]})
    return 0


# -------------------------------------------------------------------- read

#: section -> feature report, for reports the settings registry does not cover.
SECTION_RIDS = {
    **{key: rid for key, rid in MouseSession.SETTINGS_REPORTS},
    "version": p.RID_VERSION,
    "wake": p.RID_WAKE,
    "blob": p.RID_PROFILE,
    "macro": p.RID_MACRO,
}


def _read_one(mouse: MouseSession, args, section: str, quick: bool = False):
    """Fetch one section.  Raises SessionError/ProtocolError when it fails."""
    if section in SETTINGS_SECTIONS:
        return mouse.read_settings(profile=args.profile, keys=[section])[section]
    if section == "version":
        return mouse.read_version()
    if section == "wake":
        return mouse.read_wake()
    if section == "blob":
        return mouse.read_profile_blob()
    if section == "macro":
        return mouse.read_macro(args.macro)
    if section == "battery":
        if quick:
            # A full dump must not sit for the whole 3 x 2.5 s window waiting
            # for an event the mouse only sends when it feels like it.
            return mouse.battery(timeout=0.5, attempts=1)
        return mouse.battery(timeout=args.battery_timeout, attempts=args.battery_attempts)
    raise SessionError(f"cannot read {section!r}")


def cmd_read(args) -> int:
    """Read settings.  ``atksoft read`` means every section this link exposes."""
    wanted = list(args.sections or ["all"])
    full = "all" in wanted
    sections = list(READ_SECTIONS) if full else wanted
    unknown = [name for name in sections if name not in READ_CHOICES]
    if unknown:
        raise SessionError(f"unknown section(s) {', '.join(unknown)}"
                           f" -- pick from {', '.join(READ_CHOICES)}")

    data: dict = {}
    errors: dict = {}
    unsupported: list = []
    blocks: list = []
    notes: list = []
    path = ""
    try:
        with open_session(args.node, retries=args.retries) as mouse:
            path = mouse.path
            for section in sections:
                rid = SECTION_RIDS.get(section)
                if rid is not None and not mouse.supports(rid):
                    # Not a failure: a dongle simply does not carry the
                    # wiring-only reports, and 0x08/0x0c need a live link.
                    unsupported.append(section)
                    notes.append(f"{section}: not in {mouse.path}'s descriptor "
                                 f"(report {rid:#04x} is missing)")
                    continue
                try:
                    value = _read_one(mouse, args, section, quick=full)
                except (SessionError, p.ProtocolError, OSError) as error:
                    errors[section] = str(error)
                    notes.append(f"{section}: {error}")
                    continue
                if value is None:
                    unsupported.append(section)
                    notes.append(f"{section}: no answer from the mouse "
                                 f"(it may be switched off or asleep)")
                    continue
                data[section] = value.as_dict()
                blocks.append(FORMATTERS[section](value))
    except (SessionError, OSError) as error:
        # The node itself would not open (missing udev rule, unplugged dongle).
        errors["node"] = str(error)
        notes.append(str(error))

    header = f"# {path}" if path else "# no node opened"
    text = "\n".join([header, ""] + blocks + notes)
    payload = {"node": path or None, "profile": args.profile,
               "errors": errors, "no_answer": unsupported}
    payload.update(data)
    emit(args, text, payload)
    return 1 if errors else 0


# --------------------------------------------------------------------- set

def _flatten(groups) -> list:
    """``[[1, 3], [5]]`` -> ``[1, 3, 5]`` (repeatable ``--enable``/``--disable``)."""
    return [item for group in (groups or ()) for item in group]


def update_section(args, key: str, mutate) -> int:
    """Read ``key``, let ``mutate`` edit it, write it back only if it changed.

    This is the whole write strategy of the tool: the firmware stores a setting
    as one whole report, so a change starts with a read, and
    :meth:`session.MouseSession.apply` drops the write when the bytes it would
    send match what the device just handed over.
    """
    rid = SECTION_RIDS[key]
    path = ""
    with open_session(args.node, retries=args.retries) as mouse:
        path = mouse.path
        if not mouse.supports(rid):
            raise SessionError(f"{path} does not expose the {key} report "
                               f"({rid:#04x}); a live 2.4 GHz link is required")
        settings = mouse.read_settings(profile=args.profile, keys=[key])[key]
        changes = mutate(settings)
        if args.dry_run:
            frame = mouse.frame_for(settings)
            emit(args,
                 f"{key}: would write {len(frame)} bytes to report {rid:#04x}\n"
                 f"{hexdump(frame)}",
                 {"section": key, "report_id": rid, "length": len(frame),
                  "frame": frame.hex(" "), "changes": changes, "dry_run": True})
            return 0
        results = mouse.apply({key: settings}, keys=[key])

    fresh = results[0][1] if results else None
    lines = [f"# {path}"] + changes
    if not results:
        lines.append(f"{key}: already up to date, nothing written")
    elif fresh is None:
        lines.append(f"{key}: written, but the read-back confirmation failed "
                     f"(the mouse may be asleep)")
    else:
        lines += ["", FORMATTERS[key](fresh)]
    emit(args, "\n".join(lines),
         {"node": path, "section": key, "report_id": rid, "written": bool(results),
          "changes": changes, "state": fresh.as_dict() if fresh is not None else None})
    return 0


def _set_dpi(args) -> int:
    """Change DPI stages, their colours and the sensor-level switches."""
    if not any((args.stage, args.current is not None, args.enable, args.disable, args.color,
                args.lift_off is not None, args.angle_snap is not None,
                args.motion_sync is not None, args.ripple_control is not None,
                args.indicator is not None)):
        raise SessionError("nothing to change -- see 'atksoft set dpi --help'")

    def mutate(settings):
        changes = []
        for spec in args.stage or ():
            stage, value = parse_assignment(spec, "stage spec")
            value = int(value, 0)
            before = settings.stage_dpi(stage)
            settings.set_stage_dpi(stage, value)
            # The PAW3311's table only reaches some values; report what the
            # sensor will really store rather than what was asked for.
            stored = p.bytes_to_dpi(*p.dpi_to_bytes(value))
            note = "" if stored == value else f" (the sensor stores {stored})"
            changes.append(f"stage {stage}: {before} -> {value} dpi{note}")
        for stage in _flatten(args.enable):
            settings.set_stage_enabled(stage, True)
            changes.append(f"stage {stage}: on  (stages {settings.active_stages:#04x})")
        for stage in _flatten(args.disable):
            settings.set_stage_enabled(stage, False)
            changes.append(f"stage {stage}: off (stages {settings.active_stages:#04x})")
        for spec in args.color or ():
            stage, value = parse_assignment(spec, "colour spec")
            before = format_color(settings.stage_color(stage))
            settings.set_stage_color(stage, parse_rgb(value))
            changes.append(f"stage {stage}: colour {before} -> "
                           f"{format_color(settings.stage_color(stage))}")
        for label, value, attribute in (("current stage", args.current, "current_stage"),
                                        ("indicator", args.indicator, "indicator"),
                                        ("lift-off distance", args.lift_off, "lift_off_distance")):
            if value is not None:
                before = getattr(settings, attribute)
                setattr(settings, attribute, value)
                changes.append(f"{label}: {before} -> {value}")
        for label, value, attribute in (("angle snap", args.angle_snap, "angle_snap"),
                                        ("motion sync", args.motion_sync, "motion_sync"),
                                        ("ripple control", args.ripple_control, "ripple_control")):
            if value is not None:
                before = getattr(settings, attribute)
                setattr(settings, attribute, value)
                changes.append(f"{label}: {'on' if before else 'off'} -> "
                               f"{'on' if value else 'off'}")
        return changes

    return update_section(args, "dpi", mutate)


def _set_lighting(args) -> int:
    """Change the LED mode/colour, the sleep timers and the debounce time."""
    if not any((args.mode, args.rgb, args.brightness is not None, args.speed is not None,
                args.sleep is not None, args.deep_sleep is not None,
                args.debounce is not None)):
        raise SessionError("nothing to change -- see 'atksoft set lighting --help'")

    def mutate(settings):
        changes = []
        if args.mode:
            before = settings.mode_name
            settings.mode = p.LIGHT_MODES[args.mode]
            changes.append(f"mode: {before} -> {args.mode}")
        if args.rgb:
            before = format_color(settings.rgb)
            settings.rgb = parse_rgb(args.rgb)
            changes.append(f"colour: {before} -> {format_color(settings.rgb)}")
        for label, value, attribute, unit in (
            ("brightness", args.brightness, "brightness", ""),
            ("led speed", args.speed, "led_speed", ""),
            ("sleep after", args.sleep, "sleep", " min"),
            ("deep sleep", args.deep_sleep, "deep_sleep", " min"),
            ("debounce", args.debounce, "debounce", " ms"),
        ):
            if value is not None:
                before = getattr(settings, attribute)
                setattr(settings, attribute, value)
                changes.append(f"{label}: {before}{unit} -> {value}{unit}")
        return changes

    return update_section(args, "lighting", mutate)


def _set_polling(args) -> int:
    """Change the polling rate of one profile."""
    def mutate(settings):
        before = settings.hz
        settings.hz = args.hz
        return [f"polling rate: {before} Hz -> {args.hz} Hz"]

    return update_section(args, "polling", mutate)


def _set_profile(args) -> int:
    """Change which profile is live, or how many exist."""
    if args.current is None and args.count is None:
        raise SessionError("nothing to change -- pass --current N and/or --count N")

    def mutate(settings):
        changes = []
        if args.current is not None:
            changes.append(f"current profile: {settings.current} -> {args.current}")
            settings.current = args.current
        if args.count is not None:
            changes.append(f"profile count: {settings.maximum} -> {args.count}")
            settings.maximum = args.count
        return changes

    return update_section(args, "profile", mutate)


def _set_buttons(args) -> int:
    """Rebind buttons, or put the factory mapping back."""
    if not args.set and not args.reset:
        raise SessionError("nothing to change -- see 'atksoft set buttons --help'")

    def mutate(settings):
        changes = []
        if args.reset:
            for number, slot in enumerate(p.ButtonSettings.default(args.profile).slots, start=1):
                before = settings.slot(number).describe()
                settings.slots[number - 1] = slot
                changes.append(f"button {number}: {before} -> {slot.describe()}")
        for spec in args.set or ():
            number, action = parse_assignment(spec, "button spec")
            before = settings.slot(number).describe()
            settings.set_slot(number, action, allow_read_only=args.allow_read_only)
            name = p.BUTTON_NAMES.get(number, f"slot {number}")
            changes.append(f"button {number} ({name}): {before} -> "
                           f"{settings.slot(number).describe()}")
        return changes

    return update_section(args, "buttons", mutate)


# ---------------------------------------------------- battery / watch / raw

def cmd_battery(args) -> int:
    """The mouse's battery percentage, straight from its event stream."""
    with open_session(args.node, retries=args.retries) as mouse:
        event = mouse.battery(timeout=args.timeout, attempts=args.attempts)
    if event is None:
        print("atksoft: no battery event arrived -- the 2.4 GHz link only reports "
              "one while the mouse is switched on and paired", file=sys.stderr)
        return 1
    emit(args, format_battery(event), event.as_dict())
    return 0


def cmd_watch(args) -> int:
    """Stream interrupt-IN events (battery, profile changes, write replies)."""
    lines = 0
    with open_session(args.node, retries=args.retries) as mouse:
        try:
            for event in mouse.watch(seconds=args.seconds, timeout=args.timeout):
                lines += 1
                if args.json:
                    print(json.dumps(event.as_dict()), flush=True)
                else:
                    print(format_event(event), flush=True)
        except KeyboardInterrupt:
            print(f"\n{lines} event(s)", file=sys.stderr)
    return 0


def cmd_raw(args) -> int:
    """Read or write one feature report verbatim -- the debugging escape hatch."""
    rid = parse_report_id(args.report)
    with open_session(args.node, retries=args.retries) as mouse:
        if args.write:
            payload = parse_hex(args.write)
            if not args.with_id:
                payload = bytes((rid,)) + payload
            frame = mouse.write_report(payload)
            emit(args, f"wrote {report_label(rid)} ({rid:#04x}), {len(frame)} bytes\n"
                       f"{hexdump(frame)}",
                 {"report_id": rid, "length": len(frame), "frame": frame.hex(" "),
                  "written": True})
            return 0
        frame = mouse.read_report(rid, declared_length=args.declared,
                                  parameter=args.param, retries=args.retries)
        kernel = mouse.last_read
        intended = p.reply_length(frame)
        note = ""
        if kernel is not None and kernel != len(frame):
            note = (f"\nnote: the kernel reports {kernel} bytes actually arrived, "
                    f"the descriptor says {len(frame)}")
        if intended is not None and intended > len(frame):
            note += (f"\nnote: the firmware built a {intended}-byte answer (it "
                     f"echoes the declared length + 2 in byte[1]); this descriptor "
                     f"moves {len(frame)}, so the tail is cut off")
        emit(args, f"{report_label(rid)} ({rid:#04x}), {len(frame)} bytes{note}\n"
                   f"{hexdump(frame)}",
             {"report_id": rid, "length": len(frame), "frame": frame.hex(" "),
              "kernel_transfer": kernel, "reply_length": intended})
    return 0


# ----------------------------------------------------------- install-udev

def udev_status(args, source: Path, dest: Path) -> int:
    """Report whether the rule is installed and whether the nodes opened up."""
    lines = []
    installed = dest.is_file()
    lines.append(f"rule file: {dest} -- {'installed' if installed else 'MISSING'}")
    if installed:
        current = dest.read_bytes()
        if source.is_file():
            lines.append("  content: " +
                         ("identical to the copy in this checkout" if
                          current == source.read_bytes() else
                          "differs from this checkout -- re-install to update it"))
    nodes = list_nodes()
    if not nodes:
        lines.append("no mouse node found: is the dongle plugged in?")
    openable = True
    for node in nodes:
        info = os.stat(node.path)
        state = "openable" if os.access(node.path, os.R_OK | os.W_OK) else "NOT openable"
        openable = openable and os.access(node.path, os.R_OK | os.W_OK)
        lines.append(f"{node.path}  {stat.filemode(info.st_mode)}  "
                     f"uid={info.st_uid} gid={info.st_gid}  "
                     f"{'config' if is_config_node(node) else 'input'}  {state}")
    if not installed:
        lines.append("fix: sudo atksoft install-udev, then replug the dongle")
    elif not openable:
        lines.append("the rule is installed but the live nodes predate it: "
                     "unplug and replug the dongle")
    emit(args, "\n".join(lines), {"installed": installed, "openable": openable})
    return 0 if (installed and openable) else 1


def cmd_install_udev(args) -> int:
    """Install ``data/60-atksoft.rules`` into /etc/udev/rules.d and reload it."""
    source = Path(args.rules) if args.rules else RULES_SOURCE
    dest = RULES_DEST_DIR / source.name
    if args.status:
        return udev_status(args, source, dest)
    if not source.is_file():
        raise SessionError(f"rule file {source} not found -- pass --rules PATH")
    steps = [
        ["install", "-m", "0644", str(source), str(dest)],
        ["udevadm", "control", "--reload-rules"],
        ["udevadm", "trigger", "--action=add", "--subsystem-match=hidraw"],
    ]
    if args.print_only:
        print("# would run:")
        for step in steps:
            print("sudo " + " ".join(step))
        return 0
    prefix = [] if os.geteuid() == 0 else ["sudo"]
    for step in steps:
        shown = " ".join([*prefix, *step])
        print(f"$ {shown}", flush=True)
        try:
            subprocess.run([*prefix, *step], check=True)
        except FileNotFoundError:
            raise SessionError(f"{step[0]} is not installed") from None
        except subprocess.CalledProcessError as error:
            raise SessionError(f"'{shown}' failed with status {error.returncode}"
                               " -- run it by hand to see why") from None
    print("\ninstalled.  Unplug and replug the dongle (the rule only applies to")
    print("nodes created after it is in place), then check with 'atksoft list'.")
    return 0


# ------------------------------------------------------------ command line

def _common_parser(suppress: bool) -> argparse.ArgumentParser:
    """The shared ``--node/--json/--profile/--retries`` option set.

    ``suppress`` is used for the copies handed to the subparsers.  A subparser
    parses into a *fresh* namespace and copies every value over the outer one,
    so without ``SUPPRESS`` its defaults would clobber ``atksoft --json read``.
    """
    def default(value):
        return argparse.SUPPRESS if suppress else value

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--node", metavar="PATH", default=default(None),
                        help="use this /dev/hidrawN instead of searching for the mouse")
    parser.add_argument("--json", action="store_true", default=default(False),
                        help="machine-readable output")
    parser.add_argument("--profile", type=int, default=default(1), metavar="N",
                        help="profile to read or edit, 1..%d (default: 1)" % p.MAX_PROFILES)
    parser.add_argument("--retries", type=int, default=default(3), metavar="N",
                        help="read attempts before giving up (default: 3)")
    return parser


def build_parser() -> argparse.ArgumentParser:
    """The whole command line, in one place.

    Every subcommand gets the shared options through ``parents``, so both
    ``atksoft --json read`` and ``atksoft read --json`` work.
    """
    common = _common_parser(suppress=False)
    sub_common = _common_parser(suppress=True)

    parser = argparse.ArgumentParser(
        prog="atksoft", parents=[common],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Configure an Attack Shark mouse through the 2.4 GHz dongle's "
                    "vendor feature reports.",
        epilog="""\
Settings are always read first and written back whole, so one flag cannot
disturb the rest of a report.  --dry-run prints the exact bytes that would go
out and writes nothing.

examples:
  atksoft list
  atksoft read dpi lighting
  atksoft set dpi --stage 1=800 --stage 2=1600 --color 1=ff0000
  atksoft set lighting --mode breathing --rgb 00aaff --brightness 4
  atksoft set buttons --set 4=key:ctrl+c --set 7=macro:2
  atksoft set polling 500
  atksoft battery
  atksoft watch --seconds 30
  atksoft raw 0x0b
""")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    _add_read_commands(sub, sub_common)      # list, read
    _add_set_commands(sub, sub_common)       # set dpi / lighting / polling / buttons / profile
    _add_utility_commands(sub, sub_common)   # battery, watch, raw, install-udev
    return parser


def _add_read_commands(sub, common) -> None:
    """``list`` and ``read``."""
    # -- list ------------------------------------------------------------
    p_list = sub.add_parser("list", parents=[common],
                            help="show the hidraw nodes and the reports they carry")
    p_list.add_argument("--all", action="store_true",
                        help="every hidraw node on the machine, not just the mouse")
    p_list.set_defaults(func=cmd_list)

    # -- read ------------------------------------------------------------
    p_read = sub.add_parser("read", parents=[common],
                            help="read settings (default: everything this link exposes)")
    p_read.add_argument("sections", nargs="*", metavar="SECTION",
                        help="one or more of: " + ", ".join(READ_CHOICES))
    p_read.add_argument("--macro", type=int, default=1, metavar="N",
                        help="macro id for 'read macro' (default: 1)")
    p_read.add_argument("--battery-timeout", type=float, default=2.5, metavar="SECONDS",
                        help="how long to wait for a battery event (default: 2.5)")
    p_read.add_argument("--battery-attempts", type=int, default=3, metavar="N",
                        help="how many times to re-open the event window (default: 3)")
    p_read.set_defaults(func=cmd_read)


def _add_set_commands(sub, common) -> None:
    """``set`` and the five sections it edits."""
    p_set = sub.add_parser("set", parents=[common],
                           help="change one section, keeping every other byte as read")
    set_sub = p_set.add_subparsers(dest="section", metavar="SECTION", required=True)

    p_dpi = set_sub.add_parser("dpi", parents=[common],
                               help="DPI stages, their colours and the sensor switches")
    p_dpi.add_argument("--stage", action="append", metavar="N=DPI",
                       help="set one stage, repeatable, e.g. --stage 1=800")
    p_dpi.add_argument("--current", type=int, metavar="N", help="stage the DPI button selects")
    p_dpi.add_argument("--enable", action="append", type=parse_stage_list, metavar="N[,N]",
                       help="turn stages on, e.g. --enable 1,2")
    p_dpi.add_argument("--disable", action="append", type=parse_stage_list, metavar="N[,N]",
                       help="turn stages off")
    p_dpi.add_argument("--color", action="append", metavar="N=RRGGBB",
                       help="set a stage's LED colour, e.g. --color 1=ff0000")
    p_dpi.add_argument("--lift-off", type=int, choices=p.LIFT_OFF_DISTANCES, metavar="N",
                       help="lift-off distance: " + ", ".join(map(str, p.LIFT_OFF_DISTANCES)))
    p_dpi.add_argument("--indicator", type=int, metavar="N",
                       help="which stage the LED blinks when it changes")
    p_dpi.add_argument("--angle-snap", action=argparse.BooleanOptionalAction, default=None,
                       help="snap movement to the axes")
    p_dpi.add_argument("--motion-sync", action=argparse.BooleanOptionalAction, default=None,
                       help="motion sync")
    p_dpi.add_argument("--ripple-control", action=argparse.BooleanOptionalAction, default=None,
                       help="ripple control")
    p_dpi.set_defaults(func=_set_dpi)

    p_light = set_sub.add_parser("lighting", parents=[common],
                                 help="LED mode, colour, sleep timers and debounce")
    p_light.add_argument("--mode", choices=sorted(p.LIGHT_MODES), help="LED effect")
    p_light.add_argument("--rgb", metavar="RRGGBB", help="LED colour, e.g. ff00aa")
    p_light.add_argument("--brightness", type=int, metavar="N",
                         help="%d..%d (default: %d)" % (p.BRIGHTNESS_MIN, p.BRIGHTNESS_MAX,
                                                        p.BRIGHTNESS_MAX))
    p_light.add_argument("--speed", type=int, metavar="N",
                         help="%d..%d" % (p.LED_SPEED_MIN, p.LED_SPEED_MAX))
    p_light.add_argument("--sleep", type=float, metavar="MINUTES",
                         help="light-off after this long, 0.5..30 in 0.5 steps")
    p_light.add_argument("--deep-sleep", type=float, metavar="MINUTES",
                         help="deep sleep after this long, 1..%d" % p.DEEP_SLEEP_MINUTES_MAX)
    p_light.add_argument("--debounce", type=int, metavar="MS",
                         help="switch debounce, 4..50 ms in 2 ms steps")
    p_light.set_defaults(func=_set_lighting)

    p_poll = set_sub.add_parser("polling", parents=[common],
                                help="polling rate of one profile")
    p_poll.add_argument("hz", type=int, choices=p.POLL_RATES, metavar="HZ",
                        help="one of: " + ", ".join(map(str, p.POLL_RATES)))
    p_poll.set_defaults(func=_set_polling)

    p_btn = set_sub.add_parser("buttons", parents=[common], help="button mappings")
    p_btn.add_argument("--set", action="append", metavar="N=ACTION",
                       help="rebind a slot, repeatable: --set 4=key:ctrl+c, "
                            "--set 7=macro:2, --set 9=dpi-cycle")
    p_btn.add_argument("--reset", action="store_true", help="restore the factory mapping")
    p_btn.add_argument("--allow-read-only", action="store_true",
                       help="also try slot 5, which the vendor tool locks")
    p_btn.set_defaults(func=_set_buttons)

    p_prof = set_sub.add_parser("profile", parents=[common],
                                help="the live profile and the profile count")
    p_prof.add_argument("--current", type=int, metavar="N", help="make this profile live")
    p_prof.add_argument("--count", type=int, metavar="N", help="how many profiles exist")
    p_prof.set_defaults(func=_set_profile)

    for child in (p_dpi, p_light, p_poll, p_btn, p_prof):
        child.add_argument("--dry-run", action="store_true",
                           help="print the frame that would be written, write nothing")


def _add_utility_commands(sub, common) -> None:
    """``battery``, ``watch``, ``raw`` and ``install-udev``."""
    p_bat = sub.add_parser("battery", parents=[common], help="battery percentage")
    p_bat.add_argument("--timeout", type=float, default=2.5, metavar="SECONDS",
                       help="how long to wait for an event (default: 2.5)")
    p_bat.add_argument("--attempts", type=int, default=3, metavar="N",
                       help="how many windows to try (default: 3)")
    p_bat.set_defaults(func=cmd_battery)

    p_watch = sub.add_parser("watch", parents=[common], help="stream device events")
    p_watch.add_argument("--seconds", type=float, default=None, metavar="SECONDS",
                         help="stop after this long (default: until Ctrl-C)")
    p_watch.add_argument("--timeout", type=float, default=1.0, metavar="SECONDS",
                         help="event-window length (default: 1.0)")
    p_watch.set_defaults(func=cmd_watch)

    p_raw = sub.add_parser("raw", parents=[common], help="read or write one report verbatim")
    p_raw.add_argument("report", metavar="RID", help="feature report id in hex, e.g. 0x06")
    p_raw.add_argument("--write", metavar="HEX",
                       help="hex bytes to write, e.g. --write '05 06 07'; the report id "
                            "is prepended unless --with-id is given")
    p_raw.add_argument("--with-id", action="store_true",
                       help="the hex already starts with the report id")
    p_raw.add_argument("--declared", type=int, metavar="N",
                       help="override the length declared in the 0xA0 read request")
    p_raw.add_argument("--param", type=int, metavar="N",
                       help="override the 0xA0 parameter byte (profile or macro id)")
    p_raw.set_defaults(func=cmd_raw)

    # -- install-udev ----------------------------------------------------
    p_udev = sub.add_parser("install-udev", parents=[common],
                            help="install the udev rule so /dev/hidraw* needs no sudo")
    p_udev.add_argument("--rules", metavar="PATH",
                        help="rule file to install (default: %s)" % RULES_SOURCE)
    p_udev.add_argument("--print", dest="print_only", action="store_true",
                        help="show the commands instead of running them")
    p_udev.add_argument("--status", action="store_true",
                        help="report whether the rule is installed and the nodes opened")
    p_udev.set_defaults(func=cmd_install_udev)


def main(argv=None) -> int:
    """Entry point for ``atksoft`` and ``python -m atksoft``."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except (SessionError, p.ProtocolError, OSError) as error:
        # A missing udev rule, an unplugged dongle and rejected values all end
        # up here: one line on stderr, exit code 1, no traceback.
        print(f"atksoft: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":        # pragma: no cover -- exercised through main()
    sys.exit(main())




