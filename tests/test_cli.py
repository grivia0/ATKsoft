"""The command line: output, exit codes and the write path, without a mouse.

``cli.open_session`` is redirected at the fixture below, so every command runs
against the fake hidraw device from :mod:`tests.conftest`.
"""

from __future__ import annotations

import json

import pytest

from atksoft import cli
from atksoft import protocol as p


@pytest.fixture
def fake_mouse(mouse, monkeypatch):
    """``(session, device, asked_for)`` with ``cli.open_session`` redirected."""
    session, device = mouse
    asked = []

    def open_session(node=None, **kwargs):
        asked.append(node)
        return session

    monkeypatch.setattr(cli, "open_session", open_session)
    return session, device, asked


def run(capsys, *argv) -> tuple:
    """``cli.main(argv)`` -> ``(exit code, stdout, stderr)``."""
    code = cli.main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


# ------------------------------------------------------------------- list

def test_list_marks_the_config_interface(capsys, monkeypatch, mouse_factory):
    config, _ = mouse_factory()
    config.node.path = "/dev/hidraw2"
    plain, _ = mouse_factory(features={0x01: 6})
    plain.node.path = "/dev/hidraw0"
    monkeypatch.setattr(cli, "list_nodes", lambda *a, **k: [config.node, plain.node])
    # The fake nodes are "not openable" only where /dev/hidraw0 and /dev/hidraw2
    # do not happen to exist: on a machine with the udev rules installed they do,
    # and are accessible, so pin the access check for the hint to show up.
    monkeypatch.setattr(cli.os, "access", lambda *a, **k: False)

    code, out, _ = run(capsys, "list")

    assert code == 0
    assert "/dev/hidraw2" in out and "config" in out
    assert "04:dpi:52" in out
    assert "hint: sudo atksoft install-udev" in out          # fake nodes are not openable


def test_list_says_so_when_there_is_no_mouse(capsys, monkeypatch):
    monkeypatch.setattr(cli, "list_nodes", lambda *a, **k: [])

    code, _, err = run(capsys, "list")

    assert code == 1
    assert "no Attack Shark hidraw node found" in err


def test_list_json_carries_the_report_inventory(capsys, monkeypatch, mouse_factory):
    config, _ = mouse_factory()
    config.node.path = "/dev/hidraw2"
    monkeypatch.setattr(cli, "list_nodes", lambda *a, **k: [config.node])

    code, out, _ = run(capsys, "list", "--json")

    payload = json.loads(out)
    assert code == 0
    assert payload["config"] == ["/dev/hidraw2"]
    assert payload["nodes"][0]["reports"]["04"] == 52
    assert payload["nodes"][0]["config"] is True


# ------------------------------------------------------------------- read

def test_read_prints_every_exposed_section(capsys, fake_mouse):
    code, out, _ = run(capsys, "read")

    assert code == 0
    assert "dpi (profile 1):" in out
    assert "stage 1:" in out
    assert "lighting (profile 1):" in out
    assert "polling rate: 1000 Hz" in out
    assert "buttons (profile 1):" in out
    assert "Left click" in out
    assert "profile: 1 of 1" in out


def test_read_can_be_narrowed(capsys, fake_mouse):
    code, out, _ = run(capsys, "read", "polling")

    assert code == 0
    assert "polling rate" in out
    assert "dpi (" not in out


def test_read_json_is_one_object(capsys, fake_mouse):
    code, out, _ = run(capsys, "read", "dpi", "polling", "--json")

    payload = json.loads(out)
    assert code == 0
    assert payload["dpi"]["dpi"][0]["dpi"] == 800
    assert payload["polling"]["hz"] == 1000
    assert payload["node"] == "/dev/hidraw_fake"


def test_json_is_accepted_before_the_subcommand(capsys, fake_mouse):
    """A subparser must not clobber the outer ``--json`` with its own default."""
    code, out, _ = run(capsys, "--json", "read", "dpi")

    assert code == 0
    assert json.loads(out)["dpi"]["dpi"][0]["dpi"] == 800


def test_read_of_an_unknown_section_is_an_error(capsys, fake_mouse):
    code, _, err = run(capsys, "read", "bogus")

    assert code == 1
    assert "unknown section" in err


def test_read_reports_a_section_the_interface_lacks(capsys, mouse_factory, monkeypatch):
    session, _ = mouse_factory(features={p.RID_DPI: 51, p.RID_READ: 7})
    monkeypatch.setattr(cli, "open_session", lambda node=None, **kwargs: session)

    code, out, _ = run(capsys, "read", "dpi", "lighting")

    assert code == 0
    assert "dpi (profile 1):" in out
    assert "lighting: not in /dev/hidraw_fake's descriptor (report 0x05 is missing)" in out


# -------------------------------------------------------------------- set

def test_set_dpi_reports_the_change_and_writes_one_frame(capsys, fake_mouse):
    session, device, _ = fake_mouse

    code, out, _ = run(capsys, "set", "dpi", "--stage", "1=400")

    assert code == 0
    assert "stage 1: 800 -> 400 dpi" in out
    written = [frame for frame in device.writes if frame[0] == p.RID_DPI]
    assert len(written) == 1 and len(written[0]) == 52


def test_set_dpi_re_sends_the_lighting_behind_the_dpi_write(capsys, fake_mouse):
    """Picking a stage has to hand the LED to report 0x05 (see test_session).

    Report 0x04's byte 49 is a one-shot animation, so the steady colour on the
    LED only changes when 0x05 follows -- and the CLI hands over report 0x04
    alone, so the session has to go back for the lighting report.
    """
    session, device, _ = fake_mouse

    code, _, _ = run(capsys, "set", "dpi", "--current", "3")

    assert code == 0
    written = [frame[0] for frame in device.writes if frame[0] != p.RID_READ]
    assert written == [p.RID_DPI, p.RID_LIGHT]


def test_set_dpi_names_what_the_indicator_byte_does(capsys, fake_mouse):
    """``--indicator`` is byte 49: a confirmation animation, not a stage."""
    code, out, _ = run(capsys, "set", "dpi", "--indicator", "0")

    assert code == 0
    assert "confirmation animation: 2 -> 0 (no animation)" in out


def test_set_dpi_mentions_the_value_the_sensor_will_store(capsys, fake_mouse):
    """Not every DPI value survives the PAW3311's table."""
    code, out, _ = run(capsys, "set", "dpi", "--stage", "1=12345")

    assert code == 0
    assert "the sensor stores" in out


def test_set_dry_run_writes_nothing(capsys, fake_mouse):
    session, device, _ = fake_mouse

    code, out, _ = run(capsys, "set", "dpi", "--stage", "1=400", "--dry-run")

    assert code == 0
    assert "would write 52 bytes to report 0x04" in out
    # The read still happens (that is where the rest of the frame comes from),
    # but no setting goes out.
    assert [frame for frame in device.writes if frame[0] == p.RID_DPI] == []


def test_set_without_any_flag_explains_itself(capsys, fake_mouse):
    code, _, err = run(capsys, "set", "dpi")

    assert code == 1
    assert "nothing to change" in err


def test_set_rejects_a_malformed_stage(capsys, fake_mouse):
    code, _, err = run(capsys, "set", "dpi", "--stage", "1")

    assert code == 1
    assert "must look like N=VALUE" in err


def test_set_polling_sends_the_rate_byte(capsys, fake_mouse):
    session, device, _ = fake_mouse

    code, out, _ = run(capsys, "set", "polling", "500")

    assert code == 0
    assert "polling rate: 1000 Hz -> 500 Hz" in out
    frame = [frame for frame in device.writes if frame[0] == p.RID_POLL][-1]
    assert frame[p.POLL_RATE_OFFSET] == p.BYTE_BY_POLL[500]


def test_set_lighting_carries_the_colour(capsys, fake_mouse):
    session, device, _ = fake_mouse

    code, out, _ = run(capsys, "set", "lighting", "--mode", "steady", "--rgb", "ff00aa")

    assert code == 0
    assert "mode: off -> steady" in out
    assert "colour: #00ff00 -> #ff00aa" in out
    frame = [frame for frame in device.writes if frame[0] == p.RID_LIGHT][-1]
    assert tuple(frame[p.LIGHT_RGB_OFFSET:p.LIGHT_RGB_OFFSET + 3]) == (0xFF, 0x00, 0xAA)


def test_set_lighting_rejects_a_bad_colour(capsys, fake_mouse):
    code, _, err = run(capsys, "set", "lighting", "--rgb", "nope")

    assert code == 1
    assert "neither RRGGBB nor r,g,b" in err


def test_set_lighting_uses_the_mode_choices(capsys, fake_mouse):
    with pytest.raises(SystemExit) as exit_info:
        run(capsys, "set", "lighting", "--mode", "disco")

    assert exit_info.value.code == 2


def test_set_lighting_writes_the_dpi_stage_colour_mode(capsys, fake_mouse):
    """``steady-dpi`` is 0x05 -- the mode that lights the LED from report 0x04."""
    session, device, _ = fake_mouse

    code, out, _ = run(capsys, "set", "lighting", "--mode", "steady-dpi")

    assert code == 0
    assert "mode: off -> steady-dpi" in out
    frame = [frame for frame in device.writes if frame[0] == p.RID_LIGHT][-1]
    assert frame[p.LIGHT_MODE_OFFSET] == 0x50


def test_set_lighting_offers_the_dpi_modes_and_no_longer_ripple(capsys, fake_mouse):
    """``ripple``/``reactive`` were names for modes that never existed."""
    with pytest.raises(SystemExit) as exit_info:
        run(capsys, "set", "lighting", "--help")

    assert exit_info.value.code == 0
    out = capsys.readouterr().out
    assert "steady-dpi" in out and "breathing-dpi" in out
    assert "ripple" not in out and "reactive" not in out


def test_set_lighting_rejects_the_old_misnomers(capsys, fake_mouse):
    for name in ("ripple", "reactive"):
        with pytest.raises(SystemExit) as exit_info:
            run(capsys, "set", "lighting", "--mode", name)
        assert exit_info.value.code == 2


def test_set_buttons_parses_an_action_spec(capsys, fake_mouse):
    session, device, _ = fake_mouse

    code, out, _ = run(capsys, "set", "buttons", "--set", "4=key:ctrl+c")

    assert code == 0
    assert "button 4 (Side button 1): disabled -> ctrl + c" in out
    frame = [frame for frame in device.writes if frame[0] == p.RID_BUTTONS][-1]
    offset = p.BUTTON_WRITE_OFFSET[4]
    assert frame[offset] == p.ACTION_BY_NAME["keyboard-key"]


def test_set_buttons_refuses_the_locked_slot(capsys, fake_mouse):
    code, _, err = run(capsys, "set", "buttons", "--set", "5=left-click")

    assert code == 1
    assert "read-only" in err


def test_set_buttons_reset_restores_the_factory_mapping(capsys, fake_mouse):
    session, device, _ = fake_mouse

    code, out, _ = run(capsys, "set", "buttons", "--reset")

    assert code == 0
    frame = [frame for frame in device.writes if frame[0] == p.RID_BUTTONS][-1]
    offset = p.BUTTON_WRITE_OFFSET[17]
    assert frame[offset] == p.ACTION_BY_NAME["scroll-up"]


# ----------------------------------------------------- raw / install-udev

def test_raw_reads_a_hexdump(capsys, fake_mouse):
    code, out, _ = run(capsys, "raw", "04")

    assert code == 0
    assert "dpi (0x04), 52 bytes" in out
    assert "0000" in out


def test_raw_write_prepends_the_report_id(capsys, fake_mouse):
    session, device, _ = fake_mouse

    code, _, _ = run(capsys, "raw", "0x06", "--write", "01 01 02 00")

    assert code == 0
    assert device.writes[-1][0] == p.RID_POLL
    assert device.writes[-1][1] == 0x01


def test_raw_rejects_a_bad_hex_string(capsys, fake_mouse):
    code, _, err = run(capsys, "raw", "0x06", "--write", "zz")

    assert code == 1
    assert "not a hex byte string" in err


def test_install_udev_print_shows_the_commands(capsys, fake_mouse):
    code, out, _ = run(capsys, "install-udev", "--print")

    assert code == 0
    assert "sudo install -m 0644" in out
    assert "udevadm control --reload-rules" in out
    assert "60-atksoft.rules" in out


def test_install_udev_status_reports_a_missing_rule(capsys, monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "RULES_DEST_DIR", tmp_path)
    monkeypatch.setattr(cli, "list_nodes", lambda *a, **k: [])

    code, out, _ = run(capsys, "install-udev", "--status")

    assert code == 1
    assert "MISSING" in out
    assert "is the dongle plugged in?" in out
