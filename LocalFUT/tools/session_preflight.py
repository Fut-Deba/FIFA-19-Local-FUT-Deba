#!/usr/bin/env python3
"""Let a Local FUT session start beside other offline FUT tools.

FUT revival tools for FIFA 19 and for later FIFAs redirect the same EA
hostname and listen on the same local ports. Both runners refuse a redirect
they did not write, and the local server cannot start on a port another
program holds. The dispatcher runs ``prepare`` before it changes anything and
``finish`` when the session ends.

``prepare`` ends a LocalFUT19 server left over from an earlier session, stops
with the name of any other program still holding a port, and sets the other
tool's FIFA 19 redirect lines aside. ``finish`` puts them back. The lines are
saved beside the profiles before hosts changes, so a session that never
reaches ``finish`` returns them at the next start or cleanup.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
if os.fspath(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, os.fspath(TOOLS_DIR))

import run_eaapp_full_server_guarded_at_menu as runner
from probe_eaapp_local_certificate_at_menu import hosts_path, restore_hosts


SET_ASIDE_FILE = "hosts-set-aside.json"
# The hostnames either runner refuses to find in hosts.
_REDIRECT = re.compile(r"(?i)spring18\.gosredirector\.ea\.com|gosca18\.ea\.com")
_BLOCK_START = re.compile(r"(?i)^\s*#\s*BEGIN LOCALFUT19")
_BLOCK_END = re.compile(r"(?i)^\s*#\s*END LOCALFUT19")
# One hosts line with its own line break, split as the cleanup script does.
_LINE = re.compile(r"[^\r\n]*(?:\r\n|\n|\r)|[^\r\n]+$")


def split_other_tool_lines(text: str) -> tuple[str, list[str]]:
    """Separate another tool's FIFA 19 redirect lines from the rest of hosts.

    Blocks this project writes stay where they are: the runners recover them.
    """
    kept: list[str] = []
    other: list[str] = []
    inside_block = False
    for raw in _LINE.findall(text):
        line = raw.rstrip("\r\n")
        if _BLOCK_START.search(line):
            inside_block = True
        elif _BLOCK_END.search(line):
            inside_block = False
        elif (not inside_block and "localfut19" not in line.lower() and
              _REDIRECT.search(line)):
            other.append(line)
            continue
        kept.append(raw)
    return "".join(kept), other


def _held(path: Path) -> dict | None:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def set_aside(hosts: Path, held_path: Path) -> list[str]:
    """Take another tool's redirect lines out of hosts for one session.

    Returns every line held, including lines an unfinished earlier session
    still holds.
    """
    text = hosts.read_bytes().decode("latin-1")
    cleaned, found = split_other_tool_lines(text)
    held = _held(held_path)
    if not found:
        return held["lines"] if held else []
    if not cleaned.strip():
        # Writing an empty hosts file is refused, and rightly so.
        cleaned = runner.DEFAULT_WINDOWS_HOSTS.decode("latin-1")
    if held is None:
        held = {"lines": found, "original": text, "cleaned": cleaned}
    else:
        # hosts changed after a session that never finished, so there is no
        # exact earlier file to go back to: the lines are appended instead.
        held["lines"] += [line for line in found if line not in held["lines"]]
        held["original"] = held["cleaned"] = None
    # Saved before hosts changes, so the lines always exist somewhere.
    held_path.parent.mkdir(parents=True, exist_ok=True)
    held_path.write_text(json.dumps(held, indent=1), encoding="utf-8")
    restore_hosts(hosts, cleaned.encode("latin-1"))
    return held["lines"]


def put_back(hosts: Path, held_path: Path) -> list[str]:
    """Return the held lines to hosts, byte for byte when nothing else moved."""
    held = _held(held_path)
    if held is None:
        return []
    text = hosts.read_bytes().decode("latin-1")
    if held["original"] is not None and text == held["cleaned"]:
        result = held["original"]
    else:
        present = set(re.split(r"\r\n|\n|\r", text))
        missing = [line for line in held["lines"] if line not in present]
        result = text
        if missing and result and not result.endswith(("\r", "\n")):
            result += "\r\n"
        result += "".join(line + "\r\n" for line in missing)
    if result != text:
        restore_hosts(hosts, result.encode("latin-1"))
    held_path.unlink()
    return held["lines"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "finish"))
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--hosts", type=Path)
    args = parser.parse_args(argv)
    hosts = args.hosts or hosts_path()
    held_path = args.data_root / SET_ASIDE_FILE
    try:
        if args.action == "prepare":
            runner._clear_stale_localfut_servers()
            holders = runner.foreign_port_holders()
            if holders:
                # The last line printed is the reason the launcher shows.
                print(runner.describe_foreign_port_holders(holders))
                return 1
            lines = set_aside(hosts, held_path)
            heading = ("Another FUT tool's redirect lines are set aside for "
                       "this session and go back when it ends:")
        else:
            lines = put_back(hosts, held_path)
            heading = "Another FUT tool's redirect lines are back in hosts:"
    except (OSError, ValueError, KeyError, RuntimeError) as error:
        if args.action == "prepare":
            print("FUT Deba could not set aside another FUT tool's redirect "
                  "lines (%s). Close that tool and press START LOCAL FUT "
                  "again." % error)
        else:
            print("FUT Deba could not put back another FUT tool's redirect "
                  "lines (%s). They are kept safe; run "
                  "ADVANCED\\CLEANUP_OLD_REDIRECTS.cmd to put them back."
                  % error)
        return 1
    if lines:
        print(heading)
        for line in lines:
            print("  " + line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
