#!/usr/bin/env python3
"""Discover and classify local FIFA 19 installations without modifying them."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]
SERVER_DIR = ROOT / "server"
if os.fspath(SERVER_DIR) not in sys.path:
    sys.path.insert(0, os.fspath(SERVER_DIR))

from fut_compat import (KNOWN_BUILD_PROFILES, KnownBuildProfile,
                        fingerprint_summary, inspect_known_build)


REGISTRY_KEYS = (
    r"SOFTWARE\EA Games\FIFA 19",
    r"SOFTWARE\WOW6432Node\EA Games\FIFA 19",
    r"SOFTWARE\Electronic Arts\EA Sports\FIFA 19",
    r"SOFTWARE\WOW6432Node\Electronic Arts\EA Sports\FIFA 19",
)
REGISTRY_VALUE_NAMES = (
    "Install Dir", "InstallDir", "InstallLocation", "InstallPath",
)


def _registry_candidates() -> list[tuple[str, str]]:
    if os.name != "nt":
        return []
    try:
        import winreg
    except ImportError:
        return []
    values: list[tuple[str, str]] = []
    hives = ((winreg.HKEY_LOCAL_MACHINE, "HKLM"),
             (winreg.HKEY_CURRENT_USER, "HKCU"))
    views = (0, getattr(winreg, "KEY_WOW64_64KEY", 0),
             getattr(winreg, "KEY_WOW64_32KEY", 0))
    for hive, hive_name in hives:
        for key_name in REGISTRY_KEYS:
            for view in views:
                try:
                    with winreg.OpenKey(
                            hive, key_name, 0, winreg.KEY_READ | view) as key:
                        for value_name in REGISTRY_VALUE_NAMES:
                            try:
                                value, _kind = winreg.QueryValueEx(key, value_name)
                            except OSError:
                                continue
                            if value:
                                values.append((str(value),
                                               "%s registry" % hive_name))
                except OSError:
                    continue
    return values


def _common_candidates() -> list[tuple[str, str]]:
    values: list[tuple[str, str]] = []
    configured = os.environ.get("LOCALFUT19_GAME_DIR", "").strip()
    if configured:
        values.append((configured, "LOCALFUT19_GAME_DIR"))

    # The optional v1 fallback remains discoverable, but users never need to
    # copy a game there when an installed build is detected automatically.
    values.extend((os.fspath(path), "project compatibility folder") for path in (
        ROOT / "OPTIONAL_V1_GAME",
        ROOT.parent / "OPTIONAL_V1_GAME",
    ))

    if os.name == "nt":
        program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
        program_files_x86 = os.environ.get(
            "ProgramFiles(x86)", r"C:\Program Files (x86)")
        values.extend((path, "common install location") for path in (
            os.path.join(program_files, "EA Games", "FIFA 19"),
            os.path.join(program_files, "EA Games", "FIFA 19", "Game"),
            os.path.join(program_files_x86, "EA Games", "FIFA 19"),
            os.path.join(program_files, "Origin Games", "FIFA 19"),
            os.path.join(program_files_x86, "Origin Games", "FIFA 19"),
        ))
        user_libraries = set()
        for variable in ("USERPROFILE", "OneDrive"):
            user_root = os.environ.get(variable, "").strip()
            for name in ("Desktop", "Games") if user_root else ():
                library = os.path.normpath(os.path.join(user_root, name))
                key = os.path.normcase(library)
                if key in user_libraries:
                    continue
                user_libraries.add(key)
                values.extend(
                    (folder, "user library scan")
                    for folder in _scan_for_game_folders(
                        library, _SCAN_DEPTH, budget_seconds=1.0))
        values.extend(_drive_candidates())
    return values


# Launcher and store layouts that place the game outside Program Files. The
# first tester had it at D:/Games/FIFA 19, which none of the fixed locations
# above covers.
_LIBRARY_PARENTS = (
    "",
    "Games",
    "Game",
    "EA Games",
    "EA",
    os.path.join("EA Games", "EA Desktop"),
    "Origin Games",
    os.path.join("Program Files", "EA Games"),
    os.path.join("Program Files (x86)", "Origin Games"),
    os.path.join("SteamLibrary", "steamapps", "common"),
    os.path.join("Steam", "steamapps", "common"),
    "Epic Games",
    "XboxGames",
)
_GAME_FOLDER_NAMES = ("FIFA 19", "FIFA19")
# A shallow sweep catches libraries this list does not name, without walking
# whole disks.
_SCAN_DEPTH = 3
_SCAN_BUDGET_SECONDS = 4.0
_SCAN_SKIP = {
    "windows", "$recycle.bin", "system volume information", "recovery",
    "programdata", "perflogs", "appdata", "node_modules", ".git",
}


def _fixed_drive_roots() -> list[str]:
    if os.name != "nt":
        return []
    try:
        import ctypes
        mask = ctypes.windll.kernel32.GetLogicalDrives()
    except (AttributeError, OSError):
        return []
    roots = []
    for index in range(26):
        if not mask & (1 << index):
            continue
        root = "%s:\\" % chr(ord("A") + index)
        try:
            # 3 == DRIVE_FIXED: skip optical, network and removable media.
            if ctypes.windll.kernel32.GetDriveTypeW(root) != 3:
                continue
        except (AttributeError, OSError):
            continue
        roots.append(root)
    return roots


def _scan_for_game_folders(root: str, depth: int,
                           budget_seconds: float = _SCAN_BUDGET_SECONDS
                           ) -> list[str]:
    """Return directories named like a FIFA 19 install, breadth first.

    Breadth first with a wall-clock budget: a crowded drive must never turn
    detection into a long pause, and the named library layouts above already
    cover the common cases on their own.
    """
    found: list[str] = []
    deadline = time.monotonic() + budget_seconds
    frontier = [(root, 0)]
    while frontier:
        if time.monotonic() > deadline:
            break
        current, level = frontier.pop(0)
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                    name = entry.name
                    lowered = name.lower()
                    if lowered in _SCAN_SKIP or lowered.startswith("$"):
                        continue
                    if lowered in {value.lower() for value in _GAME_FOLDER_NAMES}:
                        found.append(entry.path)
                        continue
                    if level + 1 < depth:
                        frontier.append((entry.path, level + 1))
        except (PermissionError, OSError):
            continue
    return found


def _drive_candidates() -> list[tuple[str, str]]:
    values: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(path: str, source: str) -> None:
        key = os.path.normcase(os.path.normpath(path))
        if key in seen:
            return
        seen.add(key)
        values.append((path, source))

    for root in _fixed_drive_roots():
        for parent in _LIBRARY_PARENTS:
            base = os.path.join(root, parent) if parent else root
            for name in _GAME_FOLDER_NAMES:
                folder = os.path.join(base, name)
                add(folder, "game library")
                add(os.path.join(folder, "Game"), "game library")
    for root in _fixed_drive_roots():
        for folder in _scan_for_game_folders(root, _SCAN_DEPTH):
            add(folder, "drive scan")
            add(os.path.join(folder, "Game"), "drive scan")
    return values


def _resolve_executable(value: os.PathLike[str] | str) -> Path | None:
    raw = os.path.expandvars(os.path.expanduser(os.fspath(value).strip().strip('"')))
    if not raw:
        return None
    candidate = Path(raw)
    if candidate.is_file():
        return candidate.resolve() if candidate.name.lower() == "fifa19.exe" else None
    direct = candidate / "FIFA19.exe"
    if direct.is_file():
        return direct.resolve()
    for child in ("Game",) + _GAME_FOLDER_NAMES:
        nested = candidate / child / "FIFA19.exe"
        if nested.is_file():
            return nested.resolve()
    return None


def discover_candidates(
        explicit: Iterable[os.PathLike[str] | str] = (),
) -> list[dict[str, object]]:
    requested = [(os.fspath(path), "explicit --game") for path in explicit]
    requested.extend(_registry_candidates())
    requested.extend(_common_candidates())
    rows: list[dict[str, object]] = []
    seen: set[str] = set()
    for value, source in requested:
        executable = _resolve_executable(value)
        if executable is None:
            continue
        identity = os.path.normcase(os.fspath(executable))
        if identity in seen:
            continue
        seen.add(identity)
        rows.append({"executable": executable, "source": source})
    return rows


def _fingerprint_rows(inspection) -> list[dict[str, object]]:
    return [
        {
            "relativePath": row.relative_path.replace("\\", "/"),
            "sizeBytes": row.size,
            "sha256": row.sha256,
            "fileVersion": row.file_version,
            "productVersion": row.product_version,
        }
        for row in inspection.fingerprints
    ]


def resolve_installations(
        candidates: Iterable[dict[str, object]],
        requested_profile_id: str = "",
        profiles: Iterable[KnownBuildProfile] = KNOWN_BUILD_PROFILES,
) -> dict[str, object]:
    profile_rows = tuple(profiles)
    installations = []
    for candidate in candidates:
        executable = Path(candidate["executable"])
        inspection = inspect_known_build(executable, profile_rows)
        profile = inspection.profile
        installations.append({
            "gameDirectory": os.fspath(executable.parent),
            "executable": os.fspath(executable),
            "source": candidate.get("source", "candidate"),
            "known": inspection.known,
            "profileId": profile.profile_id if profile else None,
            "displayName": profile.display_name if profile else None,
            "supportStatus": profile.support_status if profile else "unknown",
            "launchStrategy": profile.launch_strategy if profile else None,
            "selectionPriority": profile.selection_priority if profile else None,
            "featureFlags": sorted(profile.feature_flags) if profile else [],
            "reason": inspection.reason,
            "fingerprints": _fingerprint_rows(inspection),
            "fingerprintSummary": fingerprint_summary(inspection),
        })

    known = [row for row in installations if row["known"]]
    selected = None
    if requested_profile_id:
        known = [row for row in known
                 if row["profileId"] == requested_profile_id]
        if len(known) == 1:
            selected = known[0]
            message = "explicitly requested exact known build selected"
        elif len(known) > 1:
            message = ("multiple installations match the requested profile; "
                       "select one with --game")
    elif known:
        highest_priority = max(int(row["selectionPriority"]) for row in known)
        preferred = [row for row in known
                     if row["selectionPriority"] == highest_priority]
        if len(preferred) == 1:
            selected = preferred[0]
            message = ("preferred newest known FIFA 19 build selected" if
                       len(known) > 1 else "exact known build selected")
        else:
            message = ("multiple known FIFA 19 installations share the "
                       "highest selection priority")

    if selected is not None:
        decision = "selected"
    elif len(known) > 1:
        decision = "ambiguous"
    elif installations:
        decision = "unsupported"
        message = ("no exact known EXE + CardsDLL pair matched" if not
                   requested_profile_id else
                   "requested profile was not found as an exact pair")
    else:
        decision = "not-found"
        message = "FIFA19.exe was not found"
    return {
        "schemaVersion": 1,
        "decision": decision,
        "message": message,
        "selected": selected,
        "installations": installations,
    }


def detect(explicit: Iterable[os.PathLike[str] | str] = (),
           requested_profile_id: str = "",
           explicit_only: bool = False) -> dict[str, object]:
    candidates = ([{"executable": executable, "source": "explicit --game"}
                   for value in explicit
                   if (executable := _resolve_executable(value)) is not None]
                  if explicit_only else discover_candidates(explicit))
    return resolve_installations(
        candidates, requested_profile_id)


def _print_human(result: dict[str, object]) -> None:
    print("LocalFUT19 FIFA 19 build detection:",
          str(result["decision"]).upper())
    for index, row in enumerate(result["installations"], 1):
        print("  [%d] %s" % (index, row["gameDirectory"]))
        if row["known"]:
            print("      %s | %s | %s" % (
                row["displayName"], row["supportStatus"],
                row["launchStrategy"]))
        else:
            print("      UNKNOWN | %s" % row["reason"])
    print("  " + str(result["message"]))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--game", action="append", default=[],
                        help="FIFA19.exe or its containing directory")
    parser.add_argument("--profile-id", default="",
                        help="select only this exact known profile")
    parser.add_argument("--explicit-only", action="store_true",
                        help="inspect only paths supplied with --game")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = detect(args.game, args.profile_id, args.explicit_only)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        _print_human(result)
    return {
        "selected": 0,
        "not-found": 2,
        "ambiguous": 3,
        "unsupported": 4,
    }[result["decision"]]


if __name__ == "__main__":
    raise SystemExit(main())
