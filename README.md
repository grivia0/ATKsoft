# ATKsoft

**An open control panel for Attack Shark mice on Linux** -- a pure-Python command
line tool and a PyQt6 GUI, written from scratch against the mouse's own HID
feature reports.  No vendor software, no Windows, no `hidapi`, no `libusb`: the
CLI and the protocol layers talk to `/dev/hidrawN` with the standard library
alone.

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![Platform: Linux](https://img.shields.io/badge/platform-Linux-lightgrey.svg)](#requirements)
[![Tests: 133 passing](https://img.shields.io/badge/tests-133%20passing-brightgreen.svg)](#tests)

Probed against the **Attack Shark X6** over its 2.4 GHz dongle (`1d57:fa60`).
The X6's dongle runs the same firmware as the **X11** (identical report
descriptors), so the packets are shared; the X11's USB-C cable mode
(`1d57:fa55`) is wired up as well, but a dongle is the only thing that has been
probed on real hardware.

## Contents

- [What you get](#what-you-get)
- [Requirements](#requirements)
- [Install](#install)
- [Quick start](#quick-start)
- [Command reference](#command-reference)
- [How a change is written](#how-a-change-is-written)
- [Where the LED colour comes from](#where-the-led-colour-comes-from)
- [Protocol notes](#protocol-notes)
- [The GUI](#the-gui)
- [Troubleshooting](#troubleshooting)
- [Layout](#layout)
- [Tests](#tests)
- [Contributing](#contributing)
- [License](#license)

## What you get

| Section | What it controls |
| --- | --- |
| **DPI** (`0x04`) | eight stages: on/off, 800--26000 DPI and a colour each; which stage is active; lift-off distance, angle snap, motion sync, ripple control |
| **Lighting** (`0x05`) | LED mode and colour, brightness, speed, light-off and deep-sleep timers, switch debounce |
| **Buttons** (`0x08`) | all 18 slots: `disabled`, `fire`, `key:alt+tab`, `macro:2`, `dpi-cycle`, ... |
| **Polling** (`0x06`) | 125 / 250 / 500 / 1000 Hz |
| **Profiles** (`0x0C`) | which profile is live, how many exist; DPI, buttons and lighting are per profile |
| **Readouts** | battery percentage, the firmware version report, and raw hex for the reports nobody has decoded yet |
| **Events** | `atksoft watch` streams what the mouse sends on its own: battery changes, DPI cycles, writes from anywhere |

Every write is a read-modify-write of one whole report: the frame is read first,
one field is edited in memory, and the frame goes back unchanged everywhere else.
A write that would not change a single byte is skipped entirely.  `--dry-run`
prints the exact bytes and writes nothing at all.

## Requirements

* **Linux.** This talks to hidraw, which only exists on Linux -- there is no
  macOS or Windows port.
* **Python 3.10 or newer** (the code uses `X | Y` type syntax).
* An Attack Shark mouse paired over its 2.4 GHz dongle, or plugged in by cable
  and switched on.  The dongle only answers while the mouse is connected to it.
* `root` **only** once, for the udev rule in the next section.  After that
  everything runs as your normal user.
* PyQt6, but only if you want the GUI (the `[gui]` extra pulls it in).

## Install

```sh
git clone https://github.com/grivia0/ATKsoft.git
cd ATKsoft
python3 -m pip install -e .            # the `atksoft` command
python3 -m pip install -e '.[gui]'     # ... plus the PyQt6 control panel
```

Or run it straight from the checkout without installing anything:

```sh
python3 -m atksoft list
python3 -m atksoft read dpi lighting
python3 -m atksoft.gui
```

To remove it again: `python3 -m pip uninstall atksoft`.

### The udev rule (one-time, needs sudo)

`/dev/hidrawN` nodes are root-only by default (`crw------- root root`), so reads
and writes fail with `EACCES` until the bundled rule is installed:

```sh
sudo atksoft install-udev          # or, before the package is installed:
sudo install -m 0644 data/60-atksoft.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
sudo udevadm trigger --action=add --subsystem-match=hidraw
```

Then **unplug and replug the dongle**: a udev rule only applies to nodes created
after it is in place.  Check the result with `atksoft install-udev --status`:

```
rule file: /etc/udev/rules.d/60-atksoft.rules -- installed
  content: identical to the copy in this checkout
/dev/hidraw2  crw-rw----  uid=0 gid=0  config  openable
/dev/hidraw0  crw-rw----  uid=0 gid=0  input  openable
```

The rule matches vendor id `1d57` plus product id `fa60` (dongle) or `fa55`
(cable), and gives those nodes `MODE="0660"` and `TAG+="uaccess"`.  If your
system has no systemd-logind seat for `uaccess` to apply to, add an explicit
owner -- the rule file itself documents that one-line change.

## Quick start

```sh
atksoft list                    # which /dev/hidrawN is the mouse
atksoft read dpi lighting       # what the mouse currently has stored
atksoft set dpi --current 2     # make stage 2 the active one
atksoft battery                 # battery percentage
atksoft gui                     # the same, with buttons
```

`atksoft list` marks the interface that carries the vendor configuration
reports; the others are keyboard/mouse input only:

```
/dev/hidraw2  1d57:fa60  if2  config  open  Beken 2.4G Wireless Device
    reports: 04:dpi:52  05:lighting:13  06:polling:9  07:wake-up-mode:8 ...
/dev/hidraw0  1d57:fa60  if0  input   open  Beken 2.4G Wireless Device
```

Nothing here hard-codes a path: `/dev/hidrawN` is not stable across replugs, so
the configuration interface is found by looking for reports `0x04` (DPI) and
`0xA0` (read grant) in each node's report descriptor.  `--node /dev/hidrawN`
pins one anyway, and `list --all` also shows nodes that are not this vendor's
(they read `needs root/udev` while they are locked down).

Reads and writes look like this -- real output, X6 over its dongle:

```console
$ atksoft read dpi
# /dev/hidraw2

dpi (profile 1):
  stage 1:    800 dpi  #ff0000
  stage 2:   1200 dpi  #00ff00
  stage 3:   1600 dpi  #0000ff  (active)
  stage 4:   3200 dpi  #ffff00
  stage 5:   6000 dpi  #00ffff
  stage 6:  26000 dpi  #ff00ff
  stage 7:      0 dpi  #ff4000  (off)
  stage 8:      0 dpi  #ffffff  (off)
  lift-off distance: 0  angle snap: off  motion sync: off  ripple control: off

$ atksoft set dpi --current 2
# /dev/hidraw2
current stage: 3 -> 2

dpi (profile 1):
  stage 1:    800 dpi  #ff0000
  stage 2:   1200 dpi  #00ff00  (active)
  ...
```

A write prints what it changed and then the device's own read-back, so the
numbers at the end are what the mouse stored, not what was asked for: the
PAW3311 snaps DPI onto its own table (`stage 3: 6400 -> 6500 dpi (the sensor
stores 6500)`).

`--dry-run` prints the exact frame and writes nothing:

```console
$ atksoft set lighting --mode steady-dpi --dry-run
lighting: would write 13 bytes to report 0x05
0000  05 0f 01 50 03 a8 00 ff 00 01 04 01 ff           |...P.........|
```

## Command reference

Every subcommand accepts `--node /dev/hidrawN` to pin a node and `--json` for
machine-readable output; both work before or after the subcommand.  The settings
commands also take `--profile N` (1..5) and `--retries N` (default 3), the number
of read attempts before a read is reported as failed.

```
atksoft list [--all]                     which node is the mouse, what it exposes
atksoft read [SECTION ...]               profile, dpi, lighting, polling, buttons,
                                         version, battery, wake, blob, macro, all
atksoft set dpi [FLAGS]                  stages, colours, sensor switches
atksoft set lighting [FLAGS]             LED mode/colour, timers, debounce
atksoft set polling HZ                   1000 | 500 | 250 | 125
atksoft set buttons [--set N=ACTION ...] rebind slots, or --reset
atksoft set profile [--current N] [--count N]
atksoft battery [--attempts N] [--timeout S]
atksoft watch [--seconds S] [--timeout S]
atksoft raw RID [--write HEX] [--declared N] [--param N]
atksoft install-udev [--print] [--status] [--rules PATH]
atksoft gui [--profile N] [--no-connect]
```

`atksoft read` with no section reads everything the link exposes, which is what
you want first: it prints one block per report and never writes anything.

```
$ atksoft read lighting polling
# /dev/hidraw2

lighting (profile 1):
  mode: breathing-dpi  colour: #00ff00
  brightness: 8/8  speed: 3/5
  sleep after: 0.5 min  deep sleep: 10 min
  debounce: 8 ms

polling rate: 1000 Hz (profile 1)
```

`--json` prints the same data as one object per run:

```
$ atksoft --json read lighting
{
  "node": "/dev/hidraw2",
  "profile": 1,
  "errors": {},
  "no_answer": [],
  "lighting": {
    "profile": 1,
    "mode": 6,
    "mode_name": "breathing-dpi",
    "led_speed": 3,
    "brightness": 8,
    "deep_sleep_minutes": 10,
    "rgb": [0, 255, 0],
    "sleep_minutes": 0.5,
    "debounce_ms": 8
  }
}
```

Sections that a command cannot read land in `errors` (with the reason) rather
than aborting the run, so one unreadable report does not hide the other eight.

### `atksoft set dpi`

```
--stage N=DPI            set one stage's DPI (repeatable), e.g. --stage 3=1600
--enable N[,N...]        switch stages on (they are off when their DPI is 0)
--disable N[,N...]       switch stages off
--color N=RRGGBB         set one stage's colour (repeatable)
--current N              which stage is active, i.e. what the DPI button cycles from
--lift-off N             lift-off distance
--angle-snap/--no-angle-snap       snap movement to the axes
--motion-sync/--no-motion-sync     motion sync
--ripple-control/--no-ripple-control
--indicator N            the confirmation animation, see the LED section below
--dry-run                print the frame instead of writing it
```

```sh
atksoft set dpi --stage 1=800 --stage 2=1600 --color 2=00ff00
atksoft set dpi --current 4                # stage 4 becomes active
atksoft set dpi --disable 7,8 --no-angle-snap
```

At least one flag is required; anything you do not mention keeps the value that
was read from the mouse.

### `atksoft set lighting`

```
--mode MODE              off, steady, breathing, neon, rainbow,
                         steady-dpi, breathing-dpi
--rgb RRGGBB             LED colour (used by every mode except the two *-dpi)
--brightness N           1..8
--speed N                1..5
--sleep MINUTES          light-off after idle, 0.5..30 in 0.5 steps
--deep-sleep MINUTES     deep sleep after idle, 1..60
--debounce MS            switch debounce, 4..50 in 2 ms steps
```

```sh
atksoft set lighting --mode steady --rgb 00aaff --brightness 4
atksoft set lighting --mode steady-dpi     # LED follows the active DPI stage
atksoft set lighting --sleep 5 --deep-sleep 30
```

`steady-dpi` and `breathing-dpi` are the two modes in which the LED is lit in
the **active DPI stage's colour** -- see
[Where the LED colour comes from](#where-the-led-colour-comes-from).

### `atksoft set polling HZ`

```sh
atksoft set polling 500
```

`HZ` must be one of `1000`, `500`, `250`, `125` -- the four rates the dongle's
report 0x06 can express.

### `atksoft set buttons`

```
--set N=ACTION     rebind slot N (repeatable)
--reset            put the factory mapping back
--allow-read-only  also try slot 5, which the vendor's own tool locks
--dry-run          print the frame instead of writing it
```

`ACTION` is one of three forms:

```
<firmware-action>               disabled, left-click, right-click, middle-click,
                                fire, forward, backward, scroll-up, scroll-down,
                                dpi-cycle, dpi-plus, dpi-minus, mode-key, ...
key:<name|usage>[+<mod>...]     key:a, key:Right Ctrl, key:0x04, key:shift+a
macro:<id>                      run a stored macro
```

Modifier names are `ctrl`, `shift`, `alt`, `win` and may sit on either side of
the key, so `key:shift+a` and `key:a+shift` are the same binding.  Key names come
from `atksoft/keynames.py` (case-insensitive); a name containing `+` has to be
given numerically (`key:0x57` is the keypad plus), and Shift itself is
`key:0xe1`.

```sh
atksoft set buttons --set 4=key:ctrl+c            # copy
atksoft set buttons --set 9=dpi-cycle --set 10=fire
atksoft set buttons --reset --dry-run             # show the factory mapping
```

Slot 5 is read-only in the vendor's tool, so changing it needs
`--allow-read-only`; `atksoft read buttons` marks it as such.

### `atksoft set profile`

```
--current N   which profile is live (what the mouse uses right now)
--count N     how many profiles the mouse keeps
```

```sh
atksoft set profile --current 2
```

### `atksoft battery`

```console
$ atksoft battery
battery: 10% (discharging)
```

The percentage is not stored in a feature report: it arrives as an interrupt-IN
event, and the 2.4 GHz link only sends one while the mouse is awake, so
`atksoft battery --attempts 5 --timeout 2` (or waving the mouse) helps when the
first one does not arrive.

### `atksoft watch`

```console
$ atksoft watch --seconds 5
battery              10% (discharging)
battery              10% (discharging)
```

Other events print as `dpi-cycle  stage 3 selected`, `profile-changed  profile 2`
or `write  0x04`, depending on what the mouse sends.

`watch` listens to the dongle's interrupt-IN stream, which is the cheapest way
to check that a change you made is being reported back.  It reads no feature
reports at all, so it is also the safest command to run while the GUI is open.

### `atksoft raw RID`

```
--write HEX     hex bytes to send (the report id is prepended)
--with-id       the hex already starts with the report id
--declared N    override the length asked for in the 0xA0 read request
--param N       override the 0xA0 parameter byte (profile or macro id)
```

```console
$ atksoft raw 0x06
polling (0x06), 9 bytes
note: the firmware built a 11-byte answer (it echoes the declared length + 2 in
byte[1]); this descriptor moves 9, so the tail is cut off
0000  06 0b 01 01 fe 00 00 00 00                       |.........|
```

This is the escape hatch for the reports with no codec -- and for checking what
the device really answers.  `atksoft raw 0x04` shows the raw DPI frame,
including byte 49 (the confirmation animation), which is worth reading back after
a write if you want to see it with your own eyes.

### `atksoft install-udev`

`--print` shows the commands instead of running them, `--status` reports whether
the rule is installed and whether the nodes are openable, and `--rules PATH`
installs a rule file from somewhere else.

### `atksoft gui`

See [The GUI](#the-gui).  `--no-connect` opens the window without reading the
mouse, and `--profile N` picks the profile the tab starts on.

## How a change is written

The firmware stores each setting as one whole frame and hands out exactly one
read per grant, so every write is a read-modify-write:

1. `SET_FEATURE a0 <rid> <len> 00 <param> 00 00 00`, wait 0.25 s, then
   `GET_FEATURE a0` -- the reply's second byte says whether the read is granted;
2. `GET_FEATURE <rid>` -- the one and only transfer;
3. edit the decoded dataclass in memory;
4. send the whole frame back, skipping the write entirely when not one byte
   changed.

After the write the frame is read back, which is what the numbers at the end of a
`set` command are: the device's own copy, not the one that was sent.

`--dry-run` on any `set` prints the exact frame that would go out.

A detail worth knowing: the vendor's own buffers are *longer* than the wire
format.  Report `0x04` is built as 56 bytes while the descriptor declares a
52-byte transfer (`0x05`: 15 vs 13).  ATKsoft trims on the way out and zero-pads
on the way in, which is why the DPI checksum lands at `[50:52]` on the wire even
though the vendor's buffer has it at `[54:56]`.  Sending the longer frame is
what makes the control endpoint stall with `EPIPE`, so the trim is not cosmetic.

## Where the LED colour comes from

Report `0x04` carries eight stage colours, and it is tempting to read them as
"the LED is this colour while the mouse is on this stage".  That is not what the
firmware does on its own: report `0x05` owns the LED, and in every mode but two
it lights the LED in that one colour and only plays `0x04`'s *one-shot
confirmation animation* as the DPI changes.  Changing the DPI then looks like it
changed nothing: the LED flashes and goes back to the steady colour.

Two modes hand the job over to the DPI tab -- `steady-dpi` and `breathing-dpi`
(`StaticDpi` and `BreathingDpi` in the vendor's own enum, `0x50`/`0x60` in
report `0x05`'s mode byte).  In those, the LED is lit in the **active stage's**
colour, which is the only way the eight colours stay on the LED:

```sh
atksoft set lighting --mode steady-dpi
```

Two details of report `0x04` decide whether a new stage, or a new stage colour,
actually reaches the LED:

* **Byte 49 is not a stage number.**  The vendor's own comment on it reads "DPI
  Active Indicator" and its reply parser calls the field `dpiIndicationType`: the
  byte selects the one-shot animation the firmware plays when it accepts the
  report.  `0` means none, `1` three quick blinks, `2` solid for about three
  seconds, and anything else is ignored -- the vendor's tool stores `2`.  ATKsoft
  keeps the byte verbatim (the `--indicator` flag, the DPI tab's *indicator* spin
  box) and names the effects in `protocol`: `DPI_INDICATOR_NONE` / `_BLINK` /
  `_FLASH`.
* **The animation only borrows the LED** and hands it straight back to report
  `0x05`, so writing the DPI report on its own leaves the LED on the colour of
  the stage it was already showing.  That is why both vendor drivers write `0x04`
  and then `0x05` as a pair on every apply, and why `session.MouseSession.apply`
  re-sends `0x05` after *any* DPI write, even when its bytes did not change.
  Without that repeat, picking a new active stage in the GUI (or
  `atksoft set dpi --current 3`) changes the sensor and leaves the LED on the
  previous stage's colour.

The repeat is deliberately one-directional: a `0x05` write never drags `0x04`
along with it, and the extra frame is only sent on interfaces that actually
declare report `0x05` (and only when they were not written anyway as part of the
same apply).

ATKsoft used to call those two modes `ripple` and `reactive`, which described the
setting about as well as a wrong name can.  The GUI now names the modes where the
colours are edited, and the DPI tab says so while the mode is one of the other
five.

## Protocol notes

The configuration interface declares 24 feature reports.  The ones with a codec,
in the order `atksoft list` prints them (the number is the transfer length in
bytes, report id included):

| Report | Name | ATKsoft |
| --- | --- | --- |
| `0x04` | dpi | full codec: eight stages, colours, sensor switches, the indicator byte |
| `0x05` | lighting | full codec: mode, colour, brightness, speed, timers, debounce |
| `0x06` | polling | full codec: 125 / 250 / 500 / 1000 Hz |
| `0x07` | wake-up mode | **raw read only** -- no published layout, see `WAKE_UP_MODE_NOTES` |
| `0x08` | button mapping | full codec: 18 slots, three bytes each, single-byte checksum |
| `0x09` | macro | reads decode; **writes are deliberately absent** (`MACRO_WRITE_NOTES`) |
| `0x0a` | profile blob | **raw read only**: 128 opaque bytes |
| `0x0b` | version | returned as a raw 8-byte report |
| `0x0c` | profile count | full codec: the live profile and how many exist |
| `0x10`, `0x22`--`0x2e` | -- | declared by the descriptor, no codec: read them with `atksoft raw` |
| `0xa0` | read grant | the read handshake, not a setting |
| `0x01`--`0x03` | interrupt IN | events: battery, DPI cycle, profile change, write confirmations |

Reports that carry a checksum share one: a plain 16-bit sum, big-endian, over
the payload that follows the 3-byte `[id, length, profile]` header (report `0x08`
uses a single byte of it instead).  Multi-byte fields are big-endian everywhere,
because the vendor's builders use `DataView`'s default `setInt16`.

The `0xA0` grant is the part most likely to trip up a re-implementation: the
firmware hands out exactly one read per grant, and only the second byte of the
`GET_FEATURE a0` reply means "granted" (byte[1] of a *read* reply, by contrast,
is that report's own declared length plus two -- which is how `atksoft raw`
notices when the descriptor moves fewer bytes than the firmware built).

Interrupt-IN report `0x03` carries the events: battery percentage and charging
state, the stage a DPI button press selected, profile changes, and a confirmation
after a write.

Two caveats, both deliberate:

* **Macro writes** are missing rather than wrong.  Report `0x09`'s writer and
  reader disagree with each other -- the writer puts a big-endian sum at page 2
  offset 10 while the reader expects a little-endian one at offset 129, and the
  writer's sum skips the byte the reader includes -- so a frame written by us
  would read back with a checksum off by the value of its last action byte.  See
  `protocol.MACRO_WRITE_NOTES` for the whole comparison; reads work and are used.
* **Wake-up mode (`0x07`) and the profile blob (`0x0a`)** have no published
  layout in the vendor's own tooling either, so they are exposed as raw reads
  (and the Advanced tab) rather than guessed at.

Everything else has been read *and* written on a real X6: `tools/verify_hardware.py`
reads every report, re-encodes it with the codec that would write it back, and
compares the two byte for byte -- a field at the wrong offset shows up there as a
run of differing bytes, which the fake device in `tests/` can never tell you
because it only replays frames the codecs themselves built.

## The GUI

```sh
atksoft gui                         # or: atksoft-gui, or python3 -m atksoft.gui
atksoft gui --node /dev/hidraw2     # pin the node instead of searching
atksoft gui --profile 2             # edit profile 2's DPI and buttons
atksoft gui --no-connect            # open the window, connect by hand
```

Six tabs, one per thing the dongle exposes:

| Tab | Report | What it edits |
| --- | --- | --- |
| DPI | `0x04` | eight stages (on/off, DPI, colour), the active stage the DPI button cycles from, lift-off distance, angle snap, motion sync, ripple control, the confirmation-animation byte |
| Lighting | `0x05` | LED mode and colour, brightness, speed, light-off and deep-sleep timers, switch debounce; `steady-dpi` and `breathing-dpi` colour the LED from the active DPI stage |
| Buttons | `0x08` | the 18 slots, written as the same specs `atksoft set buttons --set` takes (`disabled`, `fire`, `key:shift+a`, `macro:2`) |
| Device | `0x0C`/`0x06` | live profile and profile count, polling rate, plus the node's descriptor table and the battery |
| Advanced | any | read and write one report verbatim, for the reports nobody has decoded yet (`0x07`, `0x09`, `0x0A`) |
| Monitor | -- | the interrupt-IN event stream, the cheapest liveness check there is |

The window obeys the same rules as the CLI, and shows the work: it holds one
session open, runs every read and write on a worker thread so the window never
freezes mid-grant, and *Save changes* only lights up once a tab is actually
dirty.  Saving sends the whole frame and then reads it back, so the numbers on
screen afterwards are what the mouse stored -- the PAW3311 quietly snaps DPI to
its own table, for instance, and the tab says so before you save.

Macro writes, wake-up mode (`0x07`) and the profile blob (`0x0A`) are absent from
the GUI too, for the same reason as in the CLI.  The Advanced tab can read all
three.

The GUI is the one part that needs a dependency: `pip install -e '.[gui]'` (PyQt6
6.5 or newer).  The tests drive it headless on Qt's `offscreen` plugin, so they
pass over ssh.

## Troubleshooting

**`Permission denied` / `EACCES` on `/dev/hidrawN`.**  The udev rule is not
installed yet, or the dongle has not been replugged since it was.  See
[The udev rule](#the-udev-rule-one-time-needs-sudo); `atksoft install-udev
--status` tells you which of the two it is.

**`no configurable mouse found`.**  `atksoft list --all` shows every hidraw node
and its vendor:product.  If the dongle is there but no node says `config`, the
mouse is not paired (press the pairing button, check the mouse is switched on) or
it is a different model -- please
[open an issue](https://github.com/grivia0/ATKsoft/issues) with that output.

**`the dongle refused the read request, answering with 0x00`.**  The dongle only
answers while the mouse is awake and paired: it goes to sleep when it is idle,
and a nearly flat battery makes it drop out of the link entirely.  Wave the mouse
or click a button and retry (`--retries 5`), and check `atksoft battery`.

**A write reports success but nothing changed.**  Read it back first: the CLI
already prints the device's own copy after every write, and the GUI re-reads the
tab after saving.  If the read-back shows the old value, the mouse accepted the
frame but had gone to sleep mid-write -- wake it and write again.  If the
read-back shows the *new* value but the mouse behaves as before, check which
report owns the behaviour: with the LED, that is report `0x05` and its mode
(see [Where the LED colour comes from](#where-the-led-colour-comes-from)).

**Something else is talking to the dongle.**  A second process can hold the same
node and interleave its read grants with ours; the session verifies every read
against the length its grant asked for, and reports a refused grant verbatim
instead of guessing.  Close the other tool (the vendor's, or a second copy of
this one) if reads start coming back wrong.

**`ModuleNotFoundError: PyQt6`.**  `pip install -e '.[gui]'`.

**Reads are slow.**  That is the firmware, not this tool: every read is a grant,
a 0.25 s sleep and a transfer.  `atksoft read all` is nine of them.

## Layout

```
atksoft/protocol.py     the wire format: one dataclass per report, build()/from_bytes(),
                        the 0xA0 handshake, interrupt-IN event decoding, all LUTs
atksoft/hidraw.py       HIDIOCGFEATURE/HIDIOCSFEATURE ioctls, /sys report-descriptor
                        parsing, node enumeration
atksoft/session.py      MouseSession: find the config node, read one report, write one
                        back, read_settings/apply, events/battery/watch
atksoft/cli.py          argparse front end and output formatting
atksoft/gui.py          PyQt6 control panel: worker thread, one tab per report
atksoft/keynames.py     HID keyboard usage id <-> name, for button bindings
data/60-atksoft.rules   udev rule: 0660 + uaccess for this vendor's mouse
tools/verify_hardware.py  read-only round-trip check against a live mouse
tests/                  pytest suite (no hardware required)
```

The three layers under the CLI and the GUI are stdlib only, and there is no
`hidapi`/`libusb` binding anywhere: `hidraw.HidrawDevice` is a plain file
descriptor plus `fcntl.ioctl`, which is why installing this is a `pip install`
and not a build.

## Tests

```sh
python3 -m pip install -e '.[dev]'    # pytest, plus PyQt6 for the GUI tests
python3 -m pytest
```

133 tests, none of which need a mouse: the suite fakes the HID layer
(`set_feature` / `get_feature` / `read`) with the descriptor a live X6 dongle
reports, so it covers the grant handshake (including a refused grant and a
re-used one), the trim/pad rule, read-modify-write diffing, every codec's
`build()`/`from_bytes()` round trip, the CLI and the GUI.

The GUI tests need PyQt6 and run on Qt's `offscreen` platform plugin (they set
`QT_QPA_PLATFORM` themselves), so they work over ssh and in CI.  The window they
drive is wired to the same `FakeDevice`: they check that loading a tab and saving
it again writes nothing at all, that one edited DPI stage produces one frame with
every other byte exactly as it was read, and that a binding expression the CLI
would reject never reaches the wire.

Against real hardware, there is a read-only check:

```sh
python3 tools/verify_hardware.py            # every report it can read
python3 tools/verify_hardware.py --node /dev/hidraw2 --profile 1
```

It reads each report, re-encodes it with the codec that would write it back, and
compares the bytes -- the only test that can catch a field sitting at the wrong
offset, because the fake device upstream can only replay frames the codecs
themselves built.  It writes nothing: the only traffic is the `0xA0` grant dance.

## Contributing

Bug reports and test results are genuinely useful, especially from other
Attack Shark models.  For a "this does not work on my mouse" report, please
include:

* the output of `atksoft list --all` (vendor:product and the report table),
* the output of `atksoft read` (or `--json read all`), and
* what you expected to happen instead.

For a pull request:

* keep the CLI, `protocol`, `hidraw` and `session` **stdlib only** -- PyQt6 is
  only ever pulled in by the `[gui]` extra, and the tests must pass with no
  mouse and no display attached;
* make it read-modify-write: never build a frame from scratch when the device
  will hand you the current one, and never let one flag disturb another field;
* add a test for anything you touch -- `tests/test_protocol.py` for codecs,
  `tests/test_session.py` for the read/write dance, `tests/test_cli.py` and
  `tests/test_gui.py` for the front ends;
* if you find out what a reserved report means, say so in `protocol`'s docstring
  as well as in the code, and note the evidence (a `tools/verify_hardware.py`
  round trip is the strongest kind).

Adding a new report follows the same path every time: a dataclass with `build()`
and `from_bytes()` in `protocol.py`, an entry in `session.CODEC_LENGTHS`,
`session.REPORT_NAMES` and, if it is writable, `MouseSession.SETTINGS_REPORTS`,
a CLI section, then a `verify_hardware.py` run against the real device before the
README claims it works.

## License

MIT -- see [LICENSE](LICENSE).  Use it, change it, ship it; just keep the
copyright notice.

ATKsoft is not affiliated with, endorsed by, or supported by Attack Shark.  It
talks to the mouse over the interface the mouse already exposes; the report
layouts were derived from the device's own report descriptors, the public X11
protocol documentation and the packets the vendor's tooling sends.  Writes have
been kept deliberately conservative (whole-frame read-modify-write, a read-back
after every write, and no writes at all for the three reports nobody has
decoded), but it is still software talking to firmware: nothing here comes with
a warranty, and if a setting matters to you, note it down before you change it.
