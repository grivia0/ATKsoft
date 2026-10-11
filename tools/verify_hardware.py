#!/usr/bin/env python3
"""Check the codecs against a live mouse -- read-only, safe to run any time.

Every report ``MouseSession`` can edit is read, decoded, re-encoded with the
codec that would write it back, and compared byte for byte with what the
firmware actually handed over::

    python3 tools/verify_hardware.py [--node /dev/hidraw2] [--profile 1]

A field sitting at the wrong offset shows up here as a run of differing bytes,
which the fake device in ``tests/`` cannot tell you: it only ever replays frames
the codecs themselves built.  Nothing is written to the mouse -- the only
traffic is the 0xA0 read-grant dance.

Each line reports:

``wire``    the descriptor's transfer size, i.e. what hidraw will move.
``vendor``  the length the vendor's own packet builder uses.
``reply``   byte[1] of the answer.  The firmware sets that byte to the declared
            read length + 2 (confirmed on the dongle for every report), so it
            doubles as a check on the grant and as the only way to see that an
            answer was truncated: report 0x0C is 12 bytes on the device but the
            descriptor moves 6.
``codec``   whether decode -> encode reproduces the device's bytes.  Byte[1] is
            reported separately because a write frame carries the vendor's
            packet length there, not the reply's echo.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from atksoft import protocol as p                                   # noqa: E402
from atksoft.session import SessionError, open_session              # noqa: E402

#: key -> codec: what ``MouseSession.SETTINGS_REPORTS`` pairs with each report.
CODECS = {
    "dpi": p.DpiSettings,
    "lighting": p.LightingSettings,
    "polling": p.PollingRate,
    "buttons": p.ButtonSettings,
    "profile": p.ProfileSettings,
}


def decode(codec, frame):
    """Decode the way ``session.read_*`` does, including its verify flag."""
    if codec is p.PollingRate:                  # 0x06 has no checksum to verify
        return codec.from_bytes(frame)
    return codec.from_bytes(frame, verify=True)


def differing(device_frame, rebuilt) -> list:
    """``[(offset, device byte, codec byte)]``, ignoring byte[1] (see above)."""
    out = []
    width = max(len(device_frame), len(rebuilt))
    for offset in range(width):
        mine = device_frame[offset] if offset < len(device_frame) else None
        theirs = rebuilt[offset] if offset < len(rebuilt) else None
        if offset == 1 or mine == theirs:
            continue
        out.append((offset, mine, theirs))
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only codec check against the mouse in front of you.")
    parser.add_argument("--node", metavar="PATH", default=None,
                        help="configuration node (default: find it)")
    parser.add_argument("--profile", type=int, default=1, metavar="N",
                        help="profile whose per-profile reports are read")
    args = parser.parse_args(argv)

    problems = 0
    with open_session(args.node) as mouse:
        print(f"node:    {mouse.path}")
        print(f"reports: {mouse.support_report()}")
        print()
        for key, rid in mouse.SETTINGS_REPORTS:
            if not mouse.supports(rid):
                print(f"{key:<8} {rid:#04x}  not exposed by this interface")
                continue
            wire = mouse.transfer_length(rid)
            vendor = mouse.codec_length(rid) or wire
            header = f"{key:<8} {rid:#04x}  wire {wire:>3}  vendor {vendor:>3}"

            try:
                frame = bytes(mouse.read_report(rid))
            except (SessionError, p.ProtocolError) as exc:
                print(f"{header}  READ FAILED: {exc}")
                problems += 1
                continue

            intended = p.reply_length(frame)
            if intended is None:
                reply = "reply   ?  (too short to carry a length)"
            else:
                reply = f"reply {intended:>3}  (declared {intended - 2})"
                if intended != vendor + 2:
                    reply += "  != vendor + 2 (!!)"
                if intended - 2 > wire:
                    reply += (f"  {intended - 2 - wire} byte(s) never sent: the "
                              f"descriptor moves {wire}")
            print(f"{header}  {reply}")

            try:
                settings = decode(CODECS[key], mouse._padded(rid, frame))
                rebuilt = bytes(mouse.frame_for(settings))
            except (SessionError, p.ProtocolError) as exc:
                print(f"{'':<18}  DECODE FAILED: {type(exc).__name__}: {exc}")
                problems += 1
                continue

            diffs = differing(frame, rebuilt)
            if diffs:
                print(f"{'':<18}  CODEC MISMATCH ({len(diffs)} byte(s)):")
                for offset, mine, theirs in diffs[:12]:
                    shown = "absent" if mine is None else f"{mine:#04x}"
                    print(f"{'':<22}  offset {offset:>3}: device {shown:>8}  "
                          f"codec {theirs:#04x}")
                if len(diffs) > 12:
                    print(f"{'':<22}  ... and {len(diffs) - 12} more")
                problems += 1
            else:
                print(f"{'':<18}  codec matches the device byte for byte"
                      + ("" if len(frame) == len(rebuilt)
                         else f" (over the {min(len(frame), len(rebuilt))}"
                              f" bytes they share)"))
            if key == "buttons":
                # Slots 6/8 and 17/18 are order-sensitive: a device that listed
                # them reversed would make every read-modify-write swap those
                # bindings, so this is the one thing bytes cannot settle.  A
                # press can: whatever slot 6's physical button does is slot 6.
                print(f"{'':<18}  press slot 6's button to confirm: this build "
                      f"calls it {settings.slot(6).describe()!r}, the reversed "
                      f"table would call it {settings.slot(8).describe()!r}")

    print()
    print("verdict: " + ("all decode/encode round trips agree with the device"
                         if not problems else
                         f"{problems} report(s) need attention"))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
