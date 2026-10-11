# ATKsoft

An open control panel for Attack Shark mice -- written from scratch against the
mouse's own HID reports, with no vendor software involved.

Tested against the **Attack Shark X6** over its 2.4 GHz dongle (`1d57:fa60`).
The USB-C cable mode (`1d57:fa55`) is wired up as well, but a dongle is the
only thing that has been probed so far.

* `atksoft` -- the command line tool (pure stdlib: no hidapi, no libusb).
* `atksoft.gui` -- a PyQt6 front end (planned; see *Status*).

## Status

| Layer | State |
| --- | --- |
| `hidraw.py` (ioctls, report-descriptor walk) | done, exercised against the live dongle |
| `protocol.py` (frame codecs, events, checksums) | done |
| `session.py` (discovery, read/write, apply) | done, unit-tested without hardware |
| `cli.py` | done |
| End-to-end read/write on hardware | **needs the udev rule installed** (below) |
| `gui.py` (PyQt6) | not written yet |

Report 0x09 (macros) reads with an off-by-a-few checksum and its writer and
reader disagree with each other, so macro **writes** are deliberately absent
until hardware confirms a round-trip -- see `protocol.MACRO_WRITE_NOTES`.
Report 0x07 (wake-up mode) and 0x0A (profile blob) have no published layout in
the vendor tooling either, so they are exposed as raw hex reads.

## Install

The mouse's `/dev/hidrawN` nodes are root-only by default (`crw------- root
root`).  Install the bundled udev rule once:

```sh
sudo atksoft install-udev        # or, before installing the package:
sudo install -m 0644 data/60-atksoft.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
sudo udevadm trigger --action=add --subsystem-match=hidraw
```

Then **unplug and replug the dongle** -- a udev rule only applies to nodes
created after it is in place -- and check:

```sh
atksoft list
atksoft install-udev --status
```

`atksoft list` prints one line per hidraw node and marks the one interface that
carries the vendor configuration reports:

```
/dev/hidraw2  1d57:fa60  if2  config  open  Beken 2.4G Wireless Device
    reports: 04:dpi:52  05:lighting:13  06:polling:9  07:wake-up-mode:8 ...
```

The node index (`hidrawN`) is *not* stable across replugs, so nothing in this
code base hard-codes a path -- the configuration interface is found by looking
for reports `0x04` (DPI) and `0xA0` (read grant) in each node's report
descriptor.

Run it straight from a checkout:

```sh
python3 -m atksoft list            # no installation needed
pip install -e .                   # or install the `atksoft` command
pip install -e '.[gui]'            # once the GUI lands
```

## Commands

```
atksoft list [--all]                    which node is the mouse and what it exposes
atksoft read [SECTION ...]              dpi, lighting, polling, buttons, profile,
                                        version, battery, wake, blob, macro, all
atksoft set dpi ...                     stages, colours, sensor switches
atksoft set lighting ...                LED mode/colour, sleep timers, debounce
atksoft set polling 500                 polling rate
atksoft set buttons --set 4=key:ctrl+c  rebind buttons (or --reset)
atksoft set profile --current 2         the live profile / profile count
atksoft battery                         battery percentage
atksoft watch [--seconds 30]            stream events (battery, DPI cycle, writes)
atksoft raw 0x0b                        read one report verbatim
atksoft raw 0x06 --write '01 01 ff 00'  write one report verbatim
atksoft install-udev [--print|--status] install the udev rule
```

Everything takes `--json` (before or after the subcommand) and `--node PATH` to
pin a specific node.

### How a change is written

The firmware stores each setting as one whole frame and hands out exactly one
read per `0xA0` grant, so every write is a read-modify-write:

1. `SET_FEATURE a0 <rid> <len> 00 <param> 00 00 00`, sleep 0.25 s, then
   `GET_FEATURE a0` -- the reply's second byte says whether the read is granted;
2. `GET_FEATURE <rid>` -- the one and only transfer;
3. edit the decoded dataclass in memory;
4. send the whole frame back, and skip the write entirely if the bytes did not
   change.

`--dry-run` on any `set` prints the exact frame that would go out.

A detail worth knowing: the vendor's own buffers are *longer* than the wire
format.  Report `0x04` is built as 56 bytes but the descriptor declares a
52-byte transfer (`0x05`: 15 vs 13).  ATKsoft trims on the way out and
zero-pads on the way in, which is why the DPI checksum lands at `[50:52]` on
the wire whereas the vendor buffer has it at `[54:56]`.

## Layout

```
atksoft/protocol.py   the wire format: one dataclass per report, build()/from_bytes(),
                      the 0xA0 handshake, interrupt-IN event decoding, all LUTs
atksoft/hidraw.py     HIDIOCGFEATURE/HIDIOCSFEATURE ioctls, /sys report-descriptor
                      parsing, node enumeration
atksoft/session.py    MouseSession: find the config node, read one report, write one
                      back, read_settings/apply, events/battery/watch
atksoft/cli.py        argparse front end and output formatting
atksoft/keynames.py   HID keyboard usage id <-> name, for button bindings
data/60-atksoft.rules udev rule: 0660 + uaccess for this vendor's mouse
tests/                pytest suite (no hardware required)
```

## Tests

```sh
python3 -m pytest
```

The suite fakes the HID layer (`set_feature`/`get_feature`/`read`), so it covers
the grant handshake, the trim/pad rule, read-modify-write diffing and the CLI
without a mouse plugged in.
