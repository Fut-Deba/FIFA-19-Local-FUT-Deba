#!/usr/bin/env python3
"""FUT Deba desktop launcher prototype built with the Python standard library."""

from __future__ import annotations

import argparse
import ast
import ctypes
import json
import os
import queue
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog
import tkinter as tk
from tkinter import ttk
from urllib.error import URLError
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parents[1]
TOOLS_DIR = ROOT / "tools"
SERVER_DIR = ROOT / "server"
ASSET_DIR = TOOLS_DIR / "launcher_assets"
APP_NAME = "FUT Deba"
APP_VERSION = "0.3.7 Prototype"
HOME_HERO_HEIGHT = 340
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
ECONOMY_REFRESH_NOTE = (
    "To refresh Coins or Draft Tokens in FIFA, open the Store and return "
    "to the FUT menus."
)

if os.fspath(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, os.fspath(TOOLS_DIR))
if os.fspath(SERVER_DIR) not in sys.path:
    sys.path.insert(0, os.fspath(SERVER_DIR))

from detect_fifa19_build import detect
from fut_draft_config import load_draft_settings


COLORS = {
    "navy": "#071A33",
    "navy_hover": "#102B50",
    "blue": "#0B5CFF",
    "blue_dark": "#0848C8",
    "cyan": "#28B8F7",
    "background": "#F3F7FC",
    "surface": "#FFFFFF",
    "surface_alt": "#EAF2FF",
    "border": "#DCE7F5",
    "text": "#10223D",
    "muted": "#64748B",
    "success": "#158A62",
    "warning": "#B7791F",
    "danger": "#C73E4D",
}

STATUS_COLORS = {
    "neutral": ("#F4F6F9", "#DDE3EC", COLORS["muted"]),
    "progress": ("#EAF2FF", "#9FC0FF", COLORS["blue_dark"]),
    "warning": ("#FFF8E8", "#F3D18B", COLORS["warning"]),
    "success": ("#EAF8F2", "#A9DDCA", COLORS["success"]),
    "danger": ("#FFF1F2", "#F2BCC4", COLORS["danger"]),
}


def local_data_root(local_appdata: str | os.PathLike[str] | None = None) -> Path:
    base = Path(local_appdata or os.environ.get("LOCALAPPDATA", Path.home()))
    return base / "FIFA19LocalFUT"


def acquire_launcher_instance(lock_path: Path | None = None):
    """Keep one launcher window so status probes and starts cannot multiply."""
    if os.name != "nt":
        return True
    import msvcrt

    path = lock_path or local_data_root() / "launcher.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open("a+b")
    if stream.seek(0, os.SEEK_END) == 0:
        stream.write(b"\0")
        stream.flush()
    stream.seek(0)
    try:
        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        stream.close()
        return None
    return stream


def release_launcher_instance(instance) -> None:
    if instance is True or instance is None:
        return
    import msvcrt

    try:
        instance.seek(0)
        msvcrt.locking(instance.fileno(), msvcrt.LK_UNLCK, 1)
    finally:
        instance.close()


def settings_path(local_appdata: str | os.PathLike[str] | None = None) -> Path:
    return local_data_root(local_appdata) / "launcher-settings.json"


def default_settings() -> dict[str, object]:
    return {
        "gamePath": "",
        "gamePaths": [],
        "profileId": "",
        "accountMode": "NORMAL",
        "confirmClose": True,
    }


def remember_game_path(settings: dict[str, object], value: str) -> None:
    """Remember a small ordered set of manually confirmed installations."""
    candidate = str(value or "").strip().strip('"')
    if not candidate:
        return
    paths = [str(path) for path in settings.get("gamePaths", [])
             if isinstance(path, str) and path.strip()]
    identity = os.path.normcase(os.path.normpath(candidate))
    if all(os.path.normcase(os.path.normpath(path)) != identity
           for path in paths):
        paths.append(candidate)
    settings["gamePaths"] = paths[-8:]


def account_tool_message(command: str, output: object) -> str:
    text = str(output)
    return (text + "\n\n" + ECONOMY_REFRESH_NOTE
            if command in {"addcoins", "adddrafttokens"} else text)


def load_settings(path: Path | None = None) -> dict[str, object]:
    settings = default_settings()
    target = path or settings_path()
    try:
        value = json.loads(target.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, TypeError):
        return settings
    if not isinstance(value, dict):
        return settings
    game_path = value.get("gamePath")
    game_paths = value.get("gamePaths", [])
    profile_id = value.get("profileId")
    if isinstance(game_path, str):
        settings["gamePath"] = game_path
    if isinstance(game_paths, list):
        for candidate in game_paths:
            if isinstance(candidate, str):
                remember_game_path(settings, candidate)
    if isinstance(game_path, str):
        remember_game_path(settings, game_path)
    if isinstance(profile_id, str):
        settings["profileId"] = profile_id
    settings["confirmClose"] = bool(value.get("confirmClose", True))
    return settings


def save_settings(value: dict[str, object], path: Path | None = None) -> Path:
    remember_game_path(value, str(value.get("gamePath", "")))
    target = path or settings_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, target)
    return target


def profile_directory(
    installation: dict[str, object] | None,
    account_mode: str,
    local_appdata: str | os.PathLike[str] | None = None,
) -> Path:
    strategy = str((installation or {}).get(
        "launchStrategy", "eaapp-menu-guarded-bridge"))
    profile = ("v1-3865658" if strategy == "legacy-native-server"
               else "eaapp-4052077")
    if str(account_mode).upper() == "RTG":
        profile += "-rtg"
    return local_data_root(local_appdata) / "profiles" / profile


def stop_request_path(
    installation: dict[str, object] | None,
    account_mode: str,
    local_appdata: str | os.PathLike[str] | None = None,
) -> Path:
    root = profile_directory(installation, account_mode, local_appdata)
    return (root / "runtime" / "legacy.stop"
            if str((installation or {}).get("launchStrategy")) ==
            "legacy-native-server" else root / "eaapp-full.stop")


def latest_diagnostic_path(directory: Path) -> Path | None:
    try:
        candidates = [path for pattern in ("*.jsonl", "*.network.log")
                      for path in directory.glob(pattern) if path.is_file()]
    except OSError:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime,
               default=None)


def read_diagnostic(path: Path, limit: int = 160_000) -> str:
    encoding = "utf-16-le" if path.suffix.lower() == ".jsonl" else "utf-8"
    try:
        data = path.read_bytes()
        return data[-limit:].decode(encoding, errors="replace")
    except OSError:
        return ""


def diagnostic_summary(path: Path | None) -> str:
    if path is None:
        return "No diagnostic session has been recorded yet."
    text = read_diagnostic(path)
    lowered = text.lower()
    if "hosts-restored-on-exit" in lowered:
        return "Last session closed cleanly and restored Windows routing."
    markers = ("traceback", "exception", " failed", '"error"', "error:")
    for line in reversed(text.splitlines()):
        if any(marker in line.lower() for marker in markers):
            return line.strip()[:220]
    if "server ready" in lowered or "eaapp-full-server-ready" in lowered:
        return "The latest session reached the local server ready state."
    return "No explicit error was found in the latest diagnostic file."


# The places tools/detect_fifa19_build.py searches on its own. A tester whose
# game sits anywhere else selects it once with BROWSE, and the path is kept.
GAME_LOCATION_HELP = (
    "Automatic detection looks in Program Files (EA Games, Origin Games), in "
    "your Desktop and Games folders, and on every drive in the Games, "
    "EA Games, Origin Games, Steam, Epic Games and XboxGames folders, up to "
    "three folders deep. If FIFA 19 is somewhere else, move it into one of "
    "these folders or select it once with BROWSE; the launcher remembers it.")


def compatibility_notice(result: dict[str, object]) -> tuple[str, str]:
    selected = result.get("selected")
    if isinstance(selected, dict):
        strategy = str(selected.get("launchStrategy") or "")
        if strategy == "eaapp-menu-guarded-bridge":
            return (
                "Compatible EA App build selected automatically. This is the preferred, latest supported target.",
                "success",
            )
        return (
            "FIFA 19 1.0.0.0 detected. Only the EA App build "
            "(19.0.4052077.0) is fully supported. On 1.0.0.0 the local "
            "session works only with one specific installation and may fail "
            "with \"Unable to connect\" on others; if it does, use the EA App "
            "build.",
            "warning",
        )
    decision = str(result.get("decision") or "not-found")
    if decision == "unsupported":
        return (
            "Unverified FIFA 19 build detected. It may work, but this project does not cover its compatibility and Play is disabled.",
            "warning",
        )
    if decision == "ambiguous":
        return (
            "Multiple compatible installations need a manual selection before Play.",
            "warning",
        )
    return (
        "FIFA 19 was not found. " + GAME_LOCATION_HELP,
        "warning",
    )


def _literal_mapping(value: str) -> dict[str, object]:
    try:
        parsed = ast.literal_eval(value)
    except (SyntaxError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _display_number(value: object) -> str:
    try:
        return format(int(value), ",")
    except (TypeError, ValueError):
        return str(value or "0")


def format_account_info(output: str) -> str:
    """Turn the existing CLI report into a compact launcher summary."""
    rows: dict[str, str] = {}
    for line in str(output).splitlines():
        key, separator, value = line.partition(":")
        if separator:
            rows[key.strip()] = value.strip()
    if not rows:
        return str(output)

    database = rows.get("Account DB", "")
    profile = rows.get("Active profile", "")
    if not profile and database and database != "active server":
        profile = Path(database).parent.name
    record = _literal_mapping(rows.get("Record", ""))
    inventory = _literal_mapping(rows.get("Inventory", ""))
    object_grant = _literal_mapping(rows.get("Object grant", ""))
    bonus = _literal_mapping(rows.get("Bonus players", ""))
    squads = rows.get("Squads", "[]").strip("[]").replace("'", "") or "None"

    lines = [
        "Account overview",
        "Profile: %s" % (profile or "Unknown"),
        "Club: %s" % rows.get("Club", "Unknown"),
        "Coins: %s" % _display_number(rows.get("Coins", 0)),
        "Draft Tokens: %s" % _display_number(rows.get("Draft Tokens", 0)),
        "Club items: %s" % _display_number(rows.get("Club items", 0)),
        "Record: %s W  |  %s D  |  %s L" % (
            _display_number(record.get("wins", 0)),
            _display_number(record.get("draws", 0)),
            _display_number(record.get("losses", 0))),
        "Squads: %s" % squads,
    ]
    if inventory:
        lines.extend((
            "",
            "Club inventory",
            "Players %s  |  Consumables %s  |  Staff %s" % (
                _display_number(inventory.get("player", 0)),
                _display_number(inventory.get("consumable", 0)),
                _display_number(inventory.get("staff", 0))),
            "Kits %s  |  Badges %s  |  Balls %s  |  Stadiums %s  |  Managers %s" % (
                _display_number(inventory.get("kit", 0)),
                _display_number(inventory.get("badge", 0)),
                _display_number(inventory.get("ball", 0)),
                _display_number(inventory.get("stadium", 0)),
                _display_number(inventory.get("manager", 0))),
        ))
    if object_grant or bonus:
        lines.extend(("", "Setup health"))
    if object_grant:
        lines.append("Object grant: %s of %s live (%s recorded)" % (
            _display_number(object_grant.get("live", 0)),
            _display_number(object_grant.get("expected", 0)),
            _display_number(object_grant.get("recorded", 0))))
    if bonus:
        lines.append("Bonus players: %s of %s live (%s recorded)" % (
            _display_number(bonus.get("live", 0)),
            _display_number(bonus.get("expected", 0)),
            _display_number(bonus.get("recorded", 0))))
    return "\n".join(lines)


def session_visual_state(has_build: bool, server_ready: bool,
                         fifa_running: bool, action: str = ""
                         ) -> dict[str, object]:
    """Map the observed session into one consistent launcher presentation."""
    if action == "closing":
        return {"tone": "warning", "message":
                "Closing FIFA 19. LocalFUT will then restore Windows routing...",
                "startEnabled": False, "stopEnabled": False}
    if action == "stopping":
        return {"tone": "warning", "message":
                "Stopping LocalFUT safely and restoring Windows routing...",
                "startEnabled": False, "stopEnabled": False}
    if server_ready and fifa_running:
        return {"tone": "success", "message":
                "Local server ready. FIFA 19 is running; you can enter FUT.",
                "startEnabled": False, "stopEnabled": True}
    if server_ready:
        return {"tone": "progress", "message":
                "Local server ready. Waiting for FIFA 19...",
                "startEnabled": False, "stopEnabled": True}
    if fifa_running:
        return {"tone": "progress", "message":
                "FIFA 19 detected. Waiting for the local server...",
                "startEnabled": False, "stopEnabled": True}
    if action == "starting":
        return {"tone": "progress", "message":
                "Game is loading... Please wait.",
                "startEnabled": False, "stopEnabled": True}
    if action == "failed":
        return {"tone": "danger", "message":
                "LocalFUT could not start. Open Diagnostics and send "
                "launcher-action.log from %LOCALAPPDATA%\\FIFA19LocalFUT.",
                "startEnabled": has_build, "stopEnabled": False}
    if has_build:
        return {"tone": "neutral", "message":
                "Ready to start a local FUT session.",
                "startEnabled": True, "stopEnabled": False}
    return {"tone": "warning", "message":
            "Select an exact compatible FIFA 19 build before starting.",
            "startEnabled": False, "stopEnabled": False}


_HTTP_ERROR = re.compile(r"->\s+([45]\d{2})(?:\s|$)")
_TRACE_ERROR = re.compile(
    r"^(?:Traceback \(most recent call last\):|[\w.]+(?:Error|Exception):)")


def game_error_entries(directory: Path, max_files: int = 12,
                       max_entries: int = 40) -> list[str]:
    """Return bounded failure evidence from the latest diagnostic session."""
    paths: set[Path] = set()
    try:
        for pattern in ("*.native-crash.log", "*.jsonl", "*.network.log"):
            paths.update(path for path in directory.rglob(pattern)
                         if path.is_file())
        latest=max(paths,key=lambda path:path.stat().st_mtime,default=None)
        if latest is None:
            return []
        def session_name(path: Path) -> str:
            name=path.name.lower()
            for suffix in (".native-crash.log",".network.log",".jsonl"):
                if name.endswith(suffix):
                    return name[:-len(suffix)]
            return name
        latest_session=(latest.parent,session_name(latest))
        newest=sorted(
            (path for path in paths
             if (path.parent,session_name(path))==latest_session),
            key=lambda path:path.stat().st_mtime,reverse=True)[:max_files]
    except OSError:
        return []

    entries: list[str] = []
    seen: set[tuple[str, str]] = set()
    crash_sessions = {
        (path.parent, path.name[:-len(".native-crash.log")])
        for path in paths
        if path.name.lower().endswith(".native-crash.log")
    }
    for path in newest:
        try:
            stamp = datetime.fromtimestamp(path.stat().st_mtime).strftime(
                "%Y-%m-%d %H:%M")
        except OSError:
            stamp = "Unknown time"
        is_crash_file = path.name.lower().endswith(".native-crash.log")
        is_network = path.name.lower().endswith(".network.log")
        if is_crash_file:
            detail = read_diagnostic(path).strip()
            if detail:
                entries.append("%s  NATIVE CRASH  %s\n%s" % (
                    stamp, path.name, detail[:1600]))
                if len(entries) >= max_entries:
                    return entries
            continue
        for raw_line in reversed(read_diagnostic(path).splitlines()):
            line = raw_line.strip().lstrip("\ufeff")
            if not line:
                continue
            kind = ""
            detail = line
            if is_network:
                match = _HTTP_ERROR.search(line)
                if match:
                    status = int(match.group(1))
                    if status == 480:
                        continue
                    if status < 500 and "/ut/" not in line.lower():
                        continue
                    kind = "HTTP %d" % status
                elif "[fut] error " in line.lower():
                    kind = "SERVER ERROR"
            else:
                try:
                    record = json.loads(line)
                except (TypeError, ValueError):
                    if _TRACE_ERROR.match(line):
                        kind = "RUNTIME ERROR"
                else:
                    if not isinstance(record, dict):
                        continue
                    payload = record.get("payload")
                    nested = payload if isinstance(payload, dict) else {}
                    events = " ".join(str(value).lower() for value in (
                        record.get("event", ""), nested.get("event", "")))
                    has_error = bool(record.get("error") or nested.get("error"))
                    if ("native-exception" in events and
                            (path.parent, path.stem) in crash_sessions):
                        continue
                    if "thread-sample" in events:
                        kind = "FREEZE EVIDENCE"
                    elif ("native-exception" in events or "fatal" in events or
                          "freeze" in events):
                        kind = "GAME ERROR"
                    elif (has_error or "script-error" in events or
                          any(event.endswith("-failed")
                              for event in events.split())):
                        kind = "RUNTIME ERROR"
                    detail = json.dumps(record, ensure_ascii=False,
                                        separators=(",", ":"))
            if not kind:
                continue
            normalized = re.sub(r"^\d{2}:\d{2}:\d{2}\s+", "", detail)
            key = (kind, normalized)
            if key in seen:
                continue
            seen.add(key)
            entries.append("%s  %s  %s\n%s" % (
                stamp, kind, path.name, detail[:800]))
            if len(entries) >= max_entries:
                return entries
    return entries


def game_error_report(directory: Path) -> str:
    entries = game_error_entries(directory)
    return ("\n\n".join(entries) if entries else
            "No recorded game errors were found in the latest session.")


def server_profile(timeout: float = 0.9,
                   attempts: int = 2) -> dict[str, object] | None:
    for _attempt in range(max(1, attempts)):
        try:
            with urlopen("http://127.0.0.1:8199/localfut19/profile",
                         timeout=timeout) as response:
                value = json.loads(response.read().decode("utf-8"))
            return value if isinstance(value, dict) else None
        except (OSError, ValueError, URLError):
            pass
    return None


def server_health(timeout: float = 0.5, attempts: int = 1) -> bool:
    """Probe server readiness without reading the shared account database."""
    for _attempt in range(max(1, attempts)):
        try:
            with urlopen("http://127.0.0.1:8199/localfut19/health",
                         timeout=timeout) as response:
                value = json.loads(response.read().decode("utf-8"))
            if isinstance(value, dict) and value.get("ready") is True:
                return True
        except (OSError, ValueError, URLError):
            pass
    return False


def fifa_process_running() -> bool:
    if os.name != "nt":
        return False
    try:
        completed = subprocess.run(
            ["tasklist.exe", "/FI", "IMAGENAME eq FIFA19.exe", "/NH"],
            capture_output=True,
            text=True,
            timeout=3,
            creationflags=CREATE_NO_WINDOW,
            check=False,
        )
        return "fifa19.exe" in completed.stdout.lower()
    except (OSError, subprocess.SubprocessError):
        return False


def set_aside_lines(path: Path | None = None) -> list[str]:
    """Another FUT tool's redirect lines still held by an unfinished session.

    START sets them aside for the session and puts them back when it ends
    (tools/session_preflight.py). A session that was killed never got there.
    """
    try:
        held = json.loads((path or local_data_root() / "hosts-set-aside.json")
                          .read_text(encoding="utf-8"))
        return [str(line) for line in held["lines"]]
    except (OSError, ValueError, KeyError, TypeError):
        return []


def request_fifa_close() -> bool:
    """Ask the FIFA main window to close without forcibly killing the process."""
    if os.name != "nt":
        return False
    script = (
        "$closed=$false; Get-Process -Name FIFA19 -ErrorAction SilentlyContinue | "
        "ForEach-Object { if ($_.CloseMainWindow()) { $closed=$true } }; "
        "if ($closed) { exit 0 } else { exit 1 }")
    try:
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-WindowStyle", "Hidden",
             "-Command", script],
            capture_output=True, text=True, timeout=5,
            creationflags=CREATE_NO_WINDOW, check=False)
        return completed.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


# The elevated runner closes FIFA 19 on the stop request and ends it after its
# own 20 seconds. This launcher-side offer is only the fallback for a game no
# runner owns, so it waits longer than the runner does.
FIFA_CLOSE_TIMEOUT_SECONDS = 45


def close_request_ignored(action: str, fifa_running: bool,
                          requested_at: float | None, now: float,
                          timeout: float = FIFA_CLOSE_TIMEOUT_SECONDS) -> bool:
    """True once a graceful close has been pending longer than the timeout."""
    return (action == "closing" and fifa_running and bool(requested_at) and
            now - float(requested_at) > timeout)


def force_close_fifa() -> bool:
    """End FIFA 19 after the user confirmed that a close request was ignored."""
    if os.name != "nt":
        return False
    try:
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-WindowStyle", "Hidden",
             "-Command", "Stop-Process -Name FIFA19 -Force -ErrorAction Stop"],
            capture_output=True, text=True, timeout=10,
            creationflags=CREATE_NO_WINDOW, check=False)
        return completed.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


# Live v1 2026-09-15: the game needs an extra settling window after its process
# and local server are up. Keep the launcher on "Game is loading" for 30
# seconds; this does not delay either process itself.
# The EA App bridge reports ready at a network-idle main menu and needs no wait.
V1_SETTLE_SECONDS = 30


def game_settled(installation: dict[str, object] | None,
                 ready_since: float | None, now: float,
                 settle: float = V1_SETTLE_SECONDS) -> bool:
    """True once the selected build can be entered after both became ready."""
    strategy = str((installation or {}).get("launchStrategy") or "")
    if strategy != "legacy-native-server":
        return True
    return ready_since is not None and now - ready_since >= settle


# Live 2026-09-14: two testers, one per build, pressed START LOCAL FUT and
# nothing started. The elevated action runs hidden, so the dispatcher's own
# reason (UAC, Python, preflight, detection, a FIFA already running) reached
# only launcher-action.log, and after 120 seconds the launcher said "The game
# was not detected". The reason is read back from that log instead.
_START_FINISHED = re.compile(
    r"Launcher action finished with exit code (-?\d+)")
_START_ERROR_HINT = re.compile(
    r"(?i)(error|fail|not found|was not|close every|denied|required|missing|"
    r"cannot|could not|traceback|exception|unknown|timed out|within)")
_START_ERROR_NOISE = re.compile(
    r"^(\+ |At line|At [A-Za-z]:|~+$|CategoryInfo|FullyQualifiedErrorId|"
    r"Launcher action finished|Starting launcher action)")


MANUAL_CONTINUE_SIGNAL = "eaapp-continue.signal"
MANUAL_CONTINUE_AFTER_SECONDS = 40


def should_offer_manual_continue(strategy: str, launch_action: str,
                                 elapsed_seconds: float, fifa_running: bool,
                                 server_ready: bool,
                                 already_offered: bool) -> bool:
    """Whether to offer the manual "FIFA is at the main menu" override.

    On some PCs the automatic readiness check never recognizes a fully-loaded
    FIFA 19, so an EA App start waits out its whole timeout while the game sits
    at the menu (a123, 2026-09-15). Only the EA App bridge has that wait, so
    the offer is limited to it.
    """
    return (strategy == "eaapp-menu-guarded-bridge" and
            launch_action == "starting" and not already_offered and
            fifa_running and not server_ready and
            elapsed_seconds >= MANUAL_CONTINUE_AFTER_SECONDS)


def write_manual_continue_signal(data_root: Path | None = None) -> Path:
    """Tell the elevated launcher to connect to the FIFA 19 already at the menu.

    The unelevated UI writes this under the shared data root; the elevated
    dispatcher's Wait-ForEaAppBridgeProcess consumes it.
    """
    path = Path(data_root) / MANUAL_CONTINUE_SIGNAL if data_root is not None \
        else local_data_root() / MANUAL_CONTINUE_SIGNAL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(datetime.now().astimezone().isoformat(), encoding="utf-8")
    return path


def action_log_size(log_path: Path) -> int:
    """Size of the action log now, so a new attempt ignores older runs."""
    try:
        return log_path.stat().st_size
    except OSError:
        return 0


def start_failure(log_path: Path, offset: int = 0) -> str | None:
    """The reason a START written after `offset` failed, or None.

    None means the action has not failed: it is still running, or it
    finished with exit code 0. The log mixes UTF-8 lines written by the
    action script with UTF-16 output teed from the dispatcher, so NULs are
    dropped before decoding.
    """
    try:
        with log_path.open("rb") as stream:
            stream.seek(max(0, int(offset)))
            data = stream.read()
    except OSError:
        return None
    text = data.replace(b"\x00", b"").decode("utf-8", errors="replace")
    marker = text.rfind("Starting launcher action: Start")
    if marker < 0:
        return None
    lines = [line.strip().lstrip("\ufeff")
             for line in text[marker:].splitlines()]
    finished = [_START_FINISHED.search(line) for line in lines]
    codes = [int(match.group(1)) for match in finished if match]
    if not codes or codes[-1] == 0:
        return None
    reasons = []
    meaningful = []
    for line in lines:
        # Drop the ISO timestamp the action script puts before its own lines.
        line = re.sub(r"^\d{4}-\d{2}-\d{2}T\S+\s+", "", line)
        if not line or _START_ERROR_NOISE.search(line.lstrip("+ ").strip()):
            continue
        meaningful.append(line)
        if _START_ERROR_HINT.search(line):
            # PowerShell prefixes a script error with the script path.
            # PowerShell prefixes an error with the script or program name.
            reasons.append(re.sub(r"^.*?\.(?:ps1|exe)\s*:\s*", "", line))
    reason = reasons[-1] if reasons else (meaningful[-1] if meaningful else
        "the launch stopped with exit code %d" % codes[-1])
    return reason.replace("ERROR: ", "", 1)[:240]


def runtime_snapshot(profile_root: Path) -> dict[str, object]:
    diagnostics = profile_root / "diagnostics"
    latest = latest_diagnostic_path(diagnostics)
    ready = server_health()
    return {
        "fifaRunning": fifa_process_running(),
        "serverReady": ready,
        "serverProfile": None,
        "diagnostics": diagnostics,
        "latestDiagnostic": latest,
        "diagnosticSummary": diagnostic_summary(latest),
    }


def python_executable() -> Path:
    private = ROOT / ".runtime" / "Scripts" / "python.exe"
    if private.is_file():
        return private
    current = Path(sys.executable)
    console = current.with_name("python.exe")
    return console if console.is_file() else current


def run_hidden(arguments: list[str], environment: dict[str, str] | None = None) -> str:
    completed = subprocess.run(
        arguments,
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=CREATE_NO_WINDOW,
        check=False,
    )
    output = "\n".join(part.strip() for part in
                       (completed.stdout, completed.stderr) if part.strip())
    if completed.returncode:
        raise RuntimeError(output or "The command returned exit code %d." %
                           completed.returncode)
    return output or "Completed successfully."


def elevated_action(
    action: str,
    log_path: Path,
    installation: dict[str, object] | None = None,
    account_mode: str = "NORMAL",
) -> None:
    if os.name != "nt":
        raise RuntimeError("Elevated launcher actions are available on Windows only.")
    script = TOOLS_DIR / "futdeba_launcher_action.ps1"
    arguments = [
        "-NoProfile", "-WindowStyle", "Hidden", "-ExecutionPolicy", "Bypass",
        "-File", os.fspath(script), "-Action", action,
        "-LogPath", os.fspath(log_path),
    ]
    if action == "Start":
        if not installation:
            raise RuntimeError("Select a supported FIFA 19 installation first.")
        arguments.extend([
            "-GamePath", str(installation["gameDirectory"]),
            "-ProfileId", str(installation["profileId"]),
            "-AccountMode", str(account_mode).upper(),
        ])
    parameters = subprocess.list2cmdline(arguments)
    result = ctypes.windll.shell32.ShellExecuteW(
        None, "runas", "powershell.exe", parameters, os.fspath(ROOT), 0)
    if result == 5:
        raise RuntimeError(
            "Windows administrator approval was cancelled or denied. "
            "FUT Deba was not started; the launcher is still open.")
    if result <= 32:
        raise RuntimeError("Windows did not start the elevated action (code %d)." %
                           result)


def format_bytes(value: int) -> str:
    size = float(value)
    for suffix in ("B", "KB", "MB", "GB"):
        if size < 1024 or suffix == "GB":
            return ("%.2f %s" % (size, suffix)).rstrip("0").rstrip(".")
        size /= 1024
    return "%d B" % value


def initial_account_mode(argument: str | None) -> str:
    return argument if argument in {"NORMAL", "RTG"} else "NORMAL"


class FutDebaLauncher(tk.Tk):
    def __init__(self, account_mode: str | None = None) -> None:
        super().__init__()
        self.title("FUT Deba Launcher")
        self.geometry("1240x780")
        self.minsize(1080, 690)
        self.configure(bg=COLORS["background"])
        self.protocol("WM_DELETE_WINDOW", self._close)

        self.settings = load_settings()
        self.settings["accountMode"] = initial_account_mode(account_mode)
        self.installations: list[dict[str, object]] = []
        self.selected_installation: dict[str, object] | None = None
        self.runtime: dict[str, object] = {}
        self.events: queue.Queue[tuple[object, ...]] = queue.Queue()
        self.task_running = False
        self.status_pending = False
        self.status_timer: str | None = None
        self.images: dict[str, tk.PhotoImage] = {}
        self.nav_buttons: dict[str, tk.Button] = {}
        self.pages: dict[str, tk.Frame] = {}
        self.compatibility_labels: list[tk.Label] = []
        self.status_cards: dict[str, tuple[tk.Frame, tk.Label, tk.Label]] = {}
        self.start_buttons: list[ttk.Button] = []
        self.stop_buttons: list[ttk.Button] = []
        self.launch_action = ""
        self.launch_requested_at = 0.0

        self.account_mode = tk.StringVar(
            value=str(self.settings["accountMode"]))
        self.confirm_close = tk.BooleanVar(
            value=bool(self.settings["confirmClose"]))
        self.page_title = tk.StringVar(value="Home")
        self.top_status = tk.StringVar(value="Scanning local system...")
        self.install_status = tk.StringVar(value="Scanning")
        self.server_status = tk.StringVar(value="Checking")
        self.game_status = tk.StringVar(value="Checking")
        self.profile_status = tk.StringVar(
            value="ACCOUNT: %s" % self.account_mode.get())
        self.account_switch_label = tk.StringVar(
            value="SWITCH TO %s" % (
                "RTG" if self.account_mode.get() == "NORMAL" else "NORMAL"))
        self.session_status = tk.StringVar(value="No diagnostics")
        self.game_path = tk.StringVar(value=str(self.settings["gamePath"]))
        self.build_name = tk.StringVar(value="No supported build selected")
        self.build_support = tk.StringVar(value="Unknown")
        self.build_strategy = tk.StringVar(value="-")
        self.build_fingerprint = tk.StringVar(value="-")
        self.compatibility_status = tk.StringVar(
            value="Checking FIFA 19 compatibility...")
        self.diagnostics_path = tk.StringVar(value="-")
        self.diagnostics_summary = tk.StringVar(value="Waiting for status...")
        self.game_error_summary = tk.StringVar(
            value="No game error scan has run yet.")
        self.tool_output_status = tk.StringVar(value="Ready")
        self.session_message = tk.StringVar(value="Checking the local session...")
        self.coins_amount = tk.StringVar(value="1000000")
        self.tokens_amount = tk.StringVar(value="10")
        self.sbc_set_id = tk.StringVar(value="")
        self.draft_quality = tk.StringVar(
            value=str(load_draft_settings()["name"]))

        self._load_images()
        if "icon" in self.images:
            self.iconphoto(True, self.images["icon"])
        try:
            self.iconbitmap(os.fspath(ASSET_DIR / "launcher-icon-v3.ico"))
        except tk.TclError:
            pass
        self._configure_styles()
        self._build_layout()
        self.show_page("Home")
        self.after(80, self._drain_events)
        self.after(200, self.auto_detect)
        self.after(500, self._schedule_status_refresh)
        self.after(1500, self._offer_set_aside_return)

    def _load_images(self) -> None:
        for name, filename in (
                ("hero", "futdeba-hero-v2.png"),
                ("profile", "futdeba-brand-v2.png"),
                ("icon", "launcher-icon-v3.png")):
            path = ASSET_DIR / filename
            if path.is_file():
                try:
                    self.images[name] = tk.PhotoImage(file=os.fspath(path))
                except tk.TclError:
                    pass

    def _configure_styles(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TButton", font=("Segoe UI Semibold", 10),
                        padding=(14, 9))
        style.configure("Primary.TButton", background=COLORS["blue"],
                        foreground="white", bordercolor=COLORS["blue"],
                        focusthickness=0)
        style.map("Primary.TButton",
                  background=[("active", COLORS["blue_dark"]),
                              ("disabled", "#9ABAF2")])
        style.configure("Secondary.TButton", background=COLORS["surface"],
                        foreground=COLORS["text"],
                        bordercolor=COLORS["border"], focusthickness=0)
        style.map("Secondary.TButton",
                  background=[("active", COLORS["surface_alt"])])
        style.configure("Danger.TButton", background="#FFF1F2",
                        foreground=COLORS["danger"], bordercolor="#FFD6DB")
        style.map("Danger.TButton", background=[("active", "#FFE4E8")])
        style.configure("Stop.TButton", background=COLORS["danger"],
                        foreground="white", bordercolor=COLORS["danger"])
        style.map("Stop.TButton", background=[("active", "#A82E3C"),
                                               ("disabled", "#D9A7AE")])
        style.configure("Treeview", background=COLORS["surface"],
                        fieldbackground=COLORS["surface"],
                        foreground=COLORS["text"], rowheight=34,
                        borderwidth=0, font=("Segoe UI", 9))
        style.configure("Treeview.Heading", background=COLORS["surface_alt"],
                        foreground=COLORS["text"], relief="flat",
                        font=("Segoe UI Semibold", 9), padding=(8, 8))
        style.map("Treeview", background=[("selected", COLORS["blue"])],
                  foreground=[("selected", "white")])
        style.configure("TEntry", fieldbackground="white", padding=8)
        style.configure("TCombobox", fieldbackground="white", padding=7)
        style.configure("TRadiobutton", background=COLORS["surface"],
                        foreground=COLORS["text"])
        style.configure("TCheckbutton", background=COLORS["surface"],
                        foreground=COLORS["text"])

    def _build_layout(self) -> None:
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(1, weight=1)

        sidebar = tk.Frame(self, bg=COLORS["navy"], width=232)
        sidebar.grid(row=0, column=0, sticky="nsew")
        sidebar.grid_propagate(False)

        brand = tk.Frame(sidebar, bg=COLORS["navy"])
        brand.pack(fill="x", padx=24, pady=(24, 28))
        if "profile" in self.images:
            tk.Label(brand, image=self.images["profile"], bg=COLORS["navy"],
                     bd=0).pack(anchor="w", pady=(0, 12))
        tk.Label(brand, text="FUT DEBA", bg=COLORS["navy"], fg="white",
                 font=("Segoe UI Semibold", 20)).pack(anchor="w")
        tk.Label(brand, text="LOCAL FUT 19 CONTROL", bg=COLORS["navy"],
                 fg="#8EB4E8", font=("Segoe UI Semibold", 8)).pack(anchor="w")

        for name in ("Home", "Game", "Tools", "Diagnostics", "Settings"):
            button = tk.Button(
                sidebar, text=name.upper(), anchor="w", relief="flat", bd=0,
                padx=24, pady=13, cursor="hand2", bg=COLORS["navy"],
                fg="#B9CAE2", activebackground=COLORS["navy_hover"],
                activeforeground="white", font=("Segoe UI Semibold", 9),
                command=lambda page=name: self.show_page(page))
            button.pack(fill="x", padx=12, pady=2)
            self.nav_buttons[name] = button

        sidebar_bottom = tk.Frame(sidebar, bg=COLORS["navy"])
        sidebar_bottom.pack(side="bottom", fill="x", padx=12, pady=16)
        guide = tk.Button(
            sidebar_bottom, text="GUIDE", anchor="w", relief="flat", bd=0,
            padx=12, pady=11, cursor="hand2", bg=COLORS["navy_hover"],
            fg="#D9E8FF", activebackground=COLORS["blue"],
            activeforeground="white", font=("Segoe UI Semibold", 9),
            command=lambda: self.show_page("Guide"))
        guide.pack(fill="x", pady=(0, 16))
        self.nav_buttons["Guide"] = guide
        tk.Label(sidebar_bottom, textvariable=self.profile_status,
                 bg=COLORS["navy"], fg="white",
                 font=("Segoe UI Semibold", 9)).pack(anchor="w", padx=12)
        tk.Button(
            sidebar_bottom, textvariable=self.account_switch_label,
            relief="flat", bd=0, padx=10, pady=7, cursor="hand2",
            bg=COLORS["blue"], fg="white",
            activebackground=COLORS["blue_dark"], activeforeground="white",
            font=("Segoe UI Semibold", 8),
            command=self._toggle_account_mode).pack(fill="x", padx=12,
                                                     pady=(7, 10))
        tk.Label(sidebar_bottom, text="Local only  •  No telemetry",
                 bg=COLORS["navy"], fg="#7899C3",
                 font=("Segoe UI", 8)).pack(anchor="w", padx=12)
        tk.Label(sidebar_bottom, text=APP_VERSION, bg=COLORS["navy"],
                 fg="#5F82AF", font=("Segoe UI", 8)).pack(
                     anchor="w", padx=12, pady=(8, 0))

        content = tk.Frame(self, bg=COLORS["background"])
        content.grid(row=0, column=1, sticky="nsew")
        content.grid_rowconfigure(1, weight=1)
        content.grid_columnconfigure(0, weight=1)

        header = tk.Frame(content, bg=COLORS["surface"], height=76,
                          highlightthickness=1,
                          highlightbackground=COLORS["border"])
        header.grid(row=0, column=0, sticky="ew")
        header.grid_propagate(False)
        tk.Label(header, textvariable=self.page_title, bg=COLORS["surface"],
                 fg=COLORS["text"], font=("Segoe UI Semibold", 18)).pack(
                     side="left", padx=34)
        self.top_status_label = tk.Label(
            header, textvariable=self.top_status,
            bg=COLORS["surface_alt"], fg=COLORS["blue_dark"],
            font=("Segoe UI Semibold", 9), padx=13, pady=7)
        self.top_status_label.pack(side="right", padx=34)

        body = tk.Frame(content, bg=COLORS["background"])
        body.grid(row=1, column=0, sticky="nsew")
        body.grid_rowconfigure(0, weight=1)
        body.grid_columnconfigure(0, weight=1)

        builders = {
            "Home": self._build_home,
            "Game": self._build_game,
            "Tools": self._build_tools,
            "Diagnostics": self._build_diagnostics,
            "Settings": self._build_settings,
            "Guide": self._build_guide,
        }
        for name, builder in builders.items():
            page = tk.Frame(body, bg=COLORS["background"])
            page.grid(row=0, column=0, sticky="nsew")
            self.pages[name] = page
            builder(page)

    def _card(self, parent: tk.Widget, title: str, subtitle: str = "",
              subtitle_wrap: int = 360) -> tk.Frame:
        outer = tk.Frame(parent, bg=COLORS["surface"],
                         highlightthickness=1,
                         highlightbackground=COLORS["border"])
        tk.Label(outer, text=title, bg=COLORS["surface"], fg=COLORS["text"],
                 font=("Segoe UI Semibold", 11)).pack(anchor="w", padx=18,
                                                       pady=(16, 2))
        if subtitle:
            tk.Label(outer, text=subtitle, bg=COLORS["surface"],
                     fg=COLORS["muted"], font=("Segoe UI", 8),
                     wraplength=subtitle_wrap, justify="left").pack(
                         anchor="w", padx=18)
        body = tk.Frame(outer, bg=COLORS["surface"])
        body.pack(fill="both", expand=True, padx=18, pady=(12, 16))
        outer.body = body  # type: ignore[attr-defined]
        return outer

    def _status_card(self, parent: tk.Widget, label: str,
                     value: tk.StringVar, column: int) -> None:
        card = tk.Frame(parent, bg=COLORS["surface"], highlightthickness=1,
                        highlightbackground=COLORS["border"])
        card.grid(row=0, column=column, sticky="nsew",
                  padx=(0 if column == 0 else 8, 0 if column == 3 else 8))
        parent.grid_columnconfigure(column, weight=1)
        title = tk.Label(card, text=label.upper(), bg=COLORS["surface"],
                         fg=COLORS["muted"],
                         font=("Segoe UI Semibold", 8))
        title.pack(anchor="w", padx=17, pady=(14, 5))
        current = tk.Label(card, textvariable=value, bg=COLORS["surface"],
                           fg=COLORS["text"],
                           font=("Segoe UI Semibold", 12))
        current.pack(anchor="w", padx=17, pady=(0, 15))
        self.status_cards[label.lower()] = (card, title, current)

    def _paint_status_card(self, label: str, tone: str) -> None:
        widgets = self.status_cards.get(label.lower())
        if not widgets:
            return
        background, border, foreground = STATUS_COLORS[tone]
        card, title, current = widgets
        card.configure(bg=background, highlightbackground=border)
        title.configure(bg=background, fg=foreground)
        current.configure(bg=background, fg=foreground)

    def _show_session_state(self, state: dict[str, object]) -> None:
        tone = str(state["tone"])
        background, _border, foreground = STATUS_COLORS[tone]
        panel_background = COLORS["blue_dark"] if (
            self.launch_action == "starting") else background
        panel_foreground = "white" if (
            self.launch_action == "starting") else foreground
        self.session_message.set(str(state["message"]))
        self.session_panel.configure(bg=panel_background)
        self.session_message_label.configure(
            bg=panel_background, fg=panel_foreground)
        self.top_status_label.configure(bg=background, fg=foreground)
        working = tone in {"progress", "warning"} and bool(self.launch_action)
        if working:
            self.session_spinner.grid()
            self.session_spinner.start(12)
        else:
            self.session_spinner.stop()
            self.session_spinner.grid_remove()
        for button in self.start_buttons:
            button.configure(state=("normal" if state["startEnabled"] else
                                    "disabled"))
        for button in self.stop_buttons:
            enabled = bool(state["stopEnabled"])
            button.configure(state="normal" if enabled else "disabled",
                             style="Stop.TButton" if enabled else
                             "Secondary.TButton")

    def _compatibility_banner(self, parent: tk.Widget,
                              wraplength: int) -> tk.Label:
        label = tk.Label(
            parent, textvariable=self.compatibility_status,
            bg="#FFF8E8", fg=COLORS["warning"],
            font=("Segoe UI Semibold", 8), anchor="w", justify="left",
            wraplength=wraplength, padx=10, pady=8)
        self.compatibility_labels.append(label)
        return label

    def _set_compatibility(self, result: dict[str, object]) -> None:
        message, tone = compatibility_notice(result)
        self.compatibility_status.set(message)
        background = "#EAF8F2" if tone == "success" else "#FFF8E8"
        foreground = COLORS["success"] if tone == "success" else COLORS["warning"]
        for label in self.compatibility_labels:
            label.configure(bg=background, fg=foreground)

    def _build_home(self, page: tk.Frame) -> None:
        page.grid_columnconfigure(0, weight=1)
        hero = tk.Frame(page, bg=COLORS["blue"], height=HOME_HERO_HEIGHT)
        hero.grid(row=0, column=0, sticky="ew", padx=32, pady=(22, 16))
        hero.grid_propagate(False)
        hero.grid_columnconfigure(0, weight=1)
        hero.grid_columnconfigure(1, weight=0)
        hero.grid_rowconfigure(0, weight=1)

        copy = tk.Frame(hero, bg=COLORS["blue"])
        copy.grid(row=0, column=0, sticky="nsew", padx=(34, 20), pady=15)
        tk.Label(copy, text="FUT DEBA LOCAL EXPERIENCE", bg=COLORS["blue"],
                 fg="#BFD5FF", font=("Segoe UI Semibold", 9)).pack(anchor="w")
        tk.Label(copy, text="YOUR CLUB.\nYOUR RULES.", bg=COLORS["blue"],
                 fg="white", justify="left",
                 font=("Segoe UI Semibold", 24)).pack(anchor="w", pady=(7, 5))
        tk.Label(copy, text="A private, local-first control center for FIFA 19.",
                 bg=COLORS["blue"], fg="#E7F0FF",
                 font=("Segoe UI", 10)).pack(anchor="w")
        actions = tk.Frame(copy, bg=COLORS["blue"])
        actions.pack(anchor="w", pady=(6, 0))
        start_button = ttk.Button(
            actions, text="START LOCAL FUT", style="Primary.TButton",
            command=self.start_local_fut)
        start_button.pack(side="left")
        self.start_buttons.append(start_button)
        stop_button = ttk.Button(
            actions, text="STOP LOCAL FUT", style="Secondary.TButton",
            command=self.stop_local_fut, state="disabled")
        stop_button.pack(side="left", padx=(10, 0))
        self.stop_buttons.append(stop_button)
        ttk.Button(actions, text="REFRESH", style="Secondary.TButton",
                   command=self.refresh_everything).pack(side="left", padx=(10, 0))

        self.session_panel = tk.Frame(copy, bg="#EAF2FF")
        self.session_panel.pack(anchor="w", pady=(10, 0))
        self.session_spinner = ttk.Progressbar(
            self.session_panel, mode="indeterminate", length=62)
        self.session_spinner.grid(row=0, column=0, padx=(10, 8), pady=8)
        self.session_message_label = tk.Label(
            self.session_panel, textvariable=self.session_message,
            bg="#EAF2FF", fg=COLORS["blue_dark"],
            font=("Segoe UI Semibold", 8), anchor="w", justify="left",
            wraplength=520)
        self.session_message_label.grid(row=0, column=1, sticky="ew",
                                        padx=(0, 10), pady=8)
        self.session_panel.grid_columnconfigure(1, weight=1)

        if "hero" in self.images:
            tk.Label(hero, image=self.images["hero"], bg="white", bd=0).grid(
                row=0, column=1, sticky="nse", padx=0, pady=0)

        statuses = tk.Frame(page, bg=COLORS["background"])
        statuses.grid(row=1, column=0, sticky="ew", padx=32)
        self._status_card(statuses, "Installation", self.install_status, 0)
        self._status_card(statuses, "Local server", self.server_status, 1)
        self._status_card(statuses, "Game process", self.game_status, 2)
        self._status_card(statuses, "Last session", self.session_status, 3)

        lower = tk.Frame(page, bg=COLORS["background"])
        lower.grid(row=2, column=0, sticky="nsew", padx=32, pady=(20, 28))
        lower.grid_columnconfigure(0, weight=2)
        lower.grid_columnconfigure(1, weight=1)
        page.grid_rowconfigure(2, weight=1)

        overview = self._card(lower, "System overview")
        overview.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        tk.Label(overview.body, textvariable=self.build_name,
                 bg=COLORS["surface"], fg=COLORS["text"],
                 font=("Segoe UI Semibold", 12), anchor="w").pack(fill="x")
        tk.Label(overview.body, textvariable=self.game_path,
                 bg=COLORS["surface"], fg=COLORS["muted"],
                 font=("Segoe UI", 9), anchor="w", wraplength=620,
                 justify="left").pack(fill="x", pady=(5, 14))
        self._compatibility_banner(overview.body, 610).pack(fill="x",
                                                             pady=(0, 12))
        tk.Label(overview.body, textvariable=self.diagnostics_summary,
                 bg=COLORS["surface"], fg=COLORS["muted"],
                 font=("Segoe UI", 9), anchor="w", wraplength=620,
                 justify="left").pack(fill="x")

        quick = self._card(lower, "Quick actions")
        quick.grid(row=0, column=1, sticky="nsew", padx=(10, 0))
        ttk.Button(quick.body, text="OPEN DIAGNOSTICS",
                   style="Secondary.TButton", command=self.open_diagnostics).pack(
                       fill="x", pady=(0, 8))
        ttk.Button(quick.body, text="GAME CONFIGURATION",
                   style="Secondary.TButton",
                   command=lambda: self.show_page("Game")).pack(fill="x")

        legend = tk.Frame(page, bg=COLORS["background"])
        legend.grid(row=3, column=0, sticky="ew", padx=32, pady=(0, 14))
        tk.Label(legend, text="STATUS", bg=COLORS["background"],
                 fg=COLORS["muted"], font=("Segoe UI Semibold", 8)).pack(
                     side="left", padx=(0, 12))
        for text, tone in (("Idle", "neutral"), ("Working", "progress"),
                           ("Attention", "warning"), ("Ready", "success"),
                           ("Error", "danger")):
            color = STATUS_COLORS[tone][2]
            tk.Label(legend, text="●", bg=COLORS["background"], fg=color,
                     font=("Segoe UI", 9)).pack(side="left")
            tk.Label(legend, text=text, bg=COLORS["background"],
                     fg=COLORS["muted"], font=("Segoe UI", 8)).pack(
                         side="left", padx=(3, 12))

    def _build_game(self, page: tk.Frame) -> None:
        page.grid_columnconfigure(0, weight=1)
        page.grid_rowconfigure(1, weight=1)
        path_card = self._card(
            page, "FIFA 19 installation",
            "Automatic discovery prefers the newest exact supported build. You can select another exact target manually.",
            subtitle_wrap=820)
        path_card.grid(row=0, column=0, sticky="ew", padx=32, pady=(28, 16))
        path_card.body.grid_columnconfigure(0, weight=1)
        ttk.Entry(path_card.body, textvariable=self.game_path).grid(
            row=0, column=0, sticky="ew")
        ttk.Button(path_card.body, text="CHECK PATH", style="Secondary.TButton",
                   command=self.check_entered_path).grid(row=0, column=1, padx=(8, 0))
        ttk.Button(path_card.body, text="BROWSE", style="Secondary.TButton",
                   command=self.browse_game).grid(row=0, column=2, padx=(8, 0))
        ttk.Button(path_card.body, text="AUTO-DETECT", style="Primary.TButton",
                   command=lambda: self.auto_detect(force_all=True)).grid(
                       row=0, column=3, padx=(8, 0))
        self._compatibility_banner(path_card.body, 820).grid(
            row=1, column=0, columnspan=4, sticky="ew", pady=(12, 0))

        content = tk.Frame(page, bg=COLORS["background"])
        content.grid(row=1, column=0, sticky="nsew", padx=32, pady=(0, 28))
        content.grid_columnconfigure(0, weight=3)
        content.grid_columnconfigure(1, weight=2)
        content.grid_rowconfigure(0, weight=1)

        list_card = self._card(content, "Detected installations")
        list_card.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        list_card.body.grid_rowconfigure(0, weight=1)
        list_card.body.grid_columnconfigure(0, weight=1)
        self.install_tree = ttk.Treeview(
            list_card.body, columns=("build", "status", "path"),
            show="headings", selectmode="browse")
        self.install_tree.heading("build", text="BUILD")
        self.install_tree.heading("status", text="STATUS")
        self.install_tree.heading("path", text="LOCATION")
        self.install_tree.column("build", width=185, minwidth=140)
        self.install_tree.column("status", width=90, minwidth=75)
        self.install_tree.column("path", width=275, minwidth=180)
        self.install_tree.grid(row=0, column=0, sticky="nsew")
        self.install_tree.bind("<<TreeviewSelect>>", self._tree_selected)

        details = self._card(content, "Build details")
        details.grid(row=0, column=1, sticky="nsew", padx=(10, 0))
        detail_actions = tk.Frame(details.body, bg=COLORS["surface"])
        detail_actions.pack(side="bottom", fill="x")
        ttk.Button(detail_actions, text="COPY REPORT", style="Secondary.TButton",
                   command=self.copy_build_report).pack(side="left")
        ttk.Button(detail_actions, text="USE SELECTED", style="Primary.TButton",
                   command=self.use_tree_selection).pack(side="right")
        for label, variable in (
                ("Build", self.build_name),
                ("Support", self.build_support),
                ("Launch strategy", self.build_strategy),
                ("Files", self.build_fingerprint)):
            tk.Label(details.body, text=label.upper(), bg=COLORS["surface"],
                     fg=COLORS["muted"], font=("Segoe UI Semibold", 8)).pack(
                         anchor="w", pady=(0, 3))
            tk.Label(details.body, textvariable=variable,
                     bg=COLORS["surface"], fg=COLORS["text"],
                     font=("Segoe UI", 8 if label == "Files" else 9),
                     justify="left", wraplength=400,
                     anchor="w").pack(
                         anchor="w", fill="x", pady=(0, 7))

    def _build_tools(self, page: tk.Frame) -> None:
        host = page
        host.grid_columnconfigure(0, weight=1)
        host.grid_rowconfigure(0, weight=1)
        canvas = tk.Canvas(host, bg=COLORS["background"], highlightthickness=0)
        scrollbar = ttk.Scrollbar(host, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        page = tk.Frame(canvas, bg=COLORS["background"])
        window = canvas.create_window((0, 0), window=page, anchor="nw")
        page.bind("<Configure>",
                  lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>",
                    lambda event: canvas.itemconfigure(window, width=event.width))
        page.grid_columnconfigure((0, 1), weight=1)

        mode = self._card(
            page, "Account profile",
            "Normal and RTG use separate databases. The selected profile is passed to every tool and launch action.",
            subtitle_wrap=820)
        mode.grid(row=0, column=0, columnspan=2, sticky="ew", padx=32,
                  pady=(28, 16))
        ttk.Radiobutton(mode.body, text="Normal", value="NORMAL",
                        variable=self.account_mode,
                        command=self._account_mode_changed).pack(side="left")
        ttk.Radiobutton(mode.body, text="RTG", value="RTG",
                        variable=self.account_mode,
                        command=self._account_mode_changed).pack(side="left", padx=24)
        ttk.Button(mode.body, text="REFRESH ACCOUNT", style="Secondary.TButton",
                   command=lambda: self.run_account_tool("info")).pack(side="right")

        economy = self._card(
            page, "Economy",
            "Uses the existing account utility and active-server API. " +
            ECONOMY_REFRESH_NOTE)
        economy.grid(row=1, column=0, sticky="nsew", padx=(32, 10), pady=(0, 16))
        self._tool_input_row(economy.body, "Coins", self.coins_amount,
                             "ADD COINS", lambda: self._add_positive(
                                 "addcoins", self.coins_amount.get()))
        self._tool_input_row(economy.body, "Draft Tokens", self.tokens_amount,
                             "ADD TOKENS", lambda: self._add_positive(
                                 "adddrafttokens", self.tokens_amount.get()))

        draft = self._card(page, "Draft", "Quality applies when a new Draft is created.")
        draft.grid(row=1, column=1, sticky="nsew", padx=(10, 32), pady=(0, 16))
        row = tk.Frame(draft.body, bg=COLORS["surface"])
        row.pack(fill="x", pady=(0, 10))
        ttk.Combobox(row, textvariable=self.draft_quality, state="readonly",
                     values=("classic", "boosted", "creator", "insane"),
                     width=14).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="SAVE QUALITY", style="Secondary.TButton",
                   command=self.save_draft_quality).pack(side="left", padx=(8, 0))
        ttk.Button(draft.body, text="RESET CURRENT DRAFT", style="Danger.TButton",
                   command=lambda: self.confirm_reset("draft")).pack(fill="x")

        resets = self._card(page, "Account utilities", "Backup-first destructive operations are blocked while FIFA or LocalFUT is active.")
        resets.grid(row=2, column=0, sticky="nsew", padx=(32, 10), pady=(0, 28))
        sbc = tk.Frame(resets.body, bg=COLORS["surface"])
        sbc.pack(fill="x", pady=(0, 9))
        ttk.Entry(sbc, textvariable=self.sbc_set_id, width=16).pack(
            side="left", fill="x", expand=True)
        ttk.Button(sbc, text="RESET SBC", style="Danger.TButton",
                   command=lambda: self.confirm_reset("sbc")).pack(
                       side="left", padx=(8, 0))
        ttk.Button(resets.body, text="PREPARE SEASON FINAL TEST",
                   style="Secondary.TButton",
                   command=self.confirm_season_final_test).pack(
                       fill="x", pady=(0, 9))
        ttk.Button(resets.body, text="RESET CLUB ONLY", style="Danger.TButton",
                   command=lambda: self.confirm_reset("club")).pack(
                       fill="x", pady=(0, 9))
        ttk.Button(resets.body, text="RESET FULL ACCOUNT", style="Danger.TButton",
                   command=lambda: self.confirm_reset("account")).pack(fill="x")

        routing = self._card(page, "Processes and routing", "Temporary routing is installed by Play and restored when the session ends.")
        routing.grid(row=2, column=1, sticky="nsew", padx=(10, 32), pady=(0, 28))
        start_button = ttk.Button(
            routing.body, text="START LOCAL FUT", style="Primary.TButton",
            command=self.start_local_fut)
        start_button.pack(fill="x", pady=(0, 9))
        self.start_buttons.append(start_button)
        stop_button = ttk.Button(
            routing.body, text="STOP LOCAL FUT", style="Secondary.TButton",
            command=self.stop_local_fut, state="disabled")
        stop_button.pack(fill="x", pady=(0, 9))
        self.stop_buttons.append(stop_button)
        ttk.Button(routing.body, text="REMOVE LEGACY HOSTS ENTRIES",
                   style="Danger.TButton", command=self.remove_hosts).pack(fill="x")

        output = tk.Frame(page, bg=COLORS["navy"], height=31)
        output.grid(row=3, column=0, columnspan=2, sticky="ew")
        output.grid_propagate(False)
        tk.Label(output, textvariable=self.tool_output_status,
                 bg=COLORS["navy"], fg="#D9E8FF", font=("Segoe UI", 8),
                 anchor="w").pack(fill="both", expand=True, padx=32)

    def _tool_input_row(self, parent: tk.Widget, label: str,
                        variable: tk.StringVar, button_text: str,
                        command) -> None:
        tk.Label(parent, text=label.upper(), bg=COLORS["surface"],
                 fg=COLORS["muted"], font=("Segoe UI Semibold", 8)).pack(anchor="w")
        row = tk.Frame(parent, bg=COLORS["surface"])
        row.pack(fill="x", pady=(4, 10))
        ttk.Entry(row, textvariable=variable).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text=button_text, style="Secondary.TButton",
                   command=command).pack(side="left", padx=(8, 0))

    def _build_diagnostics(self, page: tk.Frame) -> None:
        page.grid_columnconfigure(0, weight=1)
        page.grid_rowconfigure(1, weight=1)
        summary = self._card(page, "Local diagnostics", "Logs remain on this computer and are never uploaded automatically.")
        summary.grid(row=0, column=0, sticky="ew", padx=32, pady=(28, 16))
        tk.Label(summary.body, textvariable=self.diagnostics_path,
                 bg=COLORS["surface"], fg=COLORS["text"],
                 font=("Segoe UI", 9), anchor="w", wraplength=800,
                 justify="left").pack(fill="x")
        tk.Label(summary.body, textvariable=self.diagnostics_summary,
                 bg=COLORS["surface"], fg=COLORS["muted"],
                 font=("Segoe UI", 9), anchor="w", wraplength=800,
                 justify="left").pack(fill="x", pady=(7, 12))
        self.game_error_summary_label = tk.Label(
            summary.body, textvariable=self.game_error_summary,
            bg=COLORS["surface"], fg=COLORS["muted"],
            font=("Segoe UI Semibold", 9), anchor="w")
        self.game_error_summary_label.pack(fill="x", pady=(0, 12))
        buttons = tk.Frame(summary.body, bg=COLORS["surface"])
        buttons.pack(fill="x")
        ttk.Button(buttons, text="OPEN FOLDER", style="Secondary.TButton",
                   command=self.open_diagnostics).pack(side="left")
        ttk.Button(buttons, text="COPY SUMMARY", style="Secondary.TButton",
                   command=self.copy_diagnostic_summary).pack(side="left", padx=8)
        ttk.Button(buttons, text="COPY GAME ERRORS", style="Secondary.TButton",
                   command=self.copy_game_errors).pack(side="left")
        ttk.Button(buttons, text="EXPORT LATEST", style="Secondary.TButton",
                   command=self.export_latest_diagnostic).pack(side="left", padx=8)
        ttk.Button(buttons, text="REFRESH", style="Primary.TButton",
                   command=self.refresh_diagnostic_view).pack(side="right")

        log_card = self._card(
            page, "Session evidence",
            "Game Errors shows only crash, freeze, runtime, server 5xx and failed FUT API evidence. Static content misses remain available in Latest report.",
            subtitle_wrap=820)
        log_card.grid(row=1, column=0, sticky="nsew", padx=32, pady=(0, 28))
        log_card.body.grid_rowconfigure(0, weight=1)
        log_card.body.grid_columnconfigure(0, weight=1)
        notebook = ttk.Notebook(log_card.body)
        notebook.grid(row=0, column=0, sticky="nsew")
        for title, attribute in (("Game errors", "game_error_text"),
                                 ("Latest report", "diagnostic_text")):
            tab = tk.Frame(notebook, bg="#0B172A")
            tab.grid_rowconfigure(0, weight=1)
            tab.grid_columnconfigure(0, weight=1)
            text = tk.Text(
                tab, bg="#0B172A", fg="#D8E7FA", insertbackground="white",
                relief="flat", borderwidth=0, font=("Consolas", 9),
                wrap="word", padx=14, pady=12, state="disabled")
            scrollbar = ttk.Scrollbar(tab, orient="vertical", command=text.yview)
            text.configure(yscrollcommand=scrollbar.set)
            text.grid(row=0, column=0, sticky="nsew")
            scrollbar.grid(row=0, column=1, sticky="ns")
            setattr(self, attribute, text)
            notebook.add(tab, text=title)

    def _build_settings(self, page: tk.Frame) -> None:
        page.grid_columnconfigure(0, weight=1)
        preferences = self._card(page, "Launcher preferences")
        preferences.grid(row=0, column=0, sticky="ew", padx=32, pady=(28, 16))
        tk.Label(preferences.body, text="DEFAULT ACCOUNT", bg=COLORS["surface"],
                 fg=COLORS["muted"], font=("Segoe UI Semibold", 8)).pack(anchor="w")
        modes = tk.Frame(preferences.body, bg=COLORS["surface"])
        modes.pack(fill="x", pady=(7, 15))
        ttk.Radiobutton(modes, text="Normal", value="NORMAL",
                        variable=self.account_mode,
                        command=self._account_mode_changed).pack(side="left")
        ttk.Radiobutton(modes, text="RTG", value="RTG",
                        variable=self.account_mode,
                        command=self._account_mode_changed).pack(side="left", padx=24)
        ttk.Checkbutton(preferences.body,
                        text="Confirm before closing while the local server is active",
                        variable=self.confirm_close,
                        command=self._save_preferences).pack(anchor="w")

        safety = self._card(
            page, "Safety and privacy",
            "The launcher itself runs as a standard user. Windows elevation is requested only for the protected game session or hosts cleanup.",
            subtitle_wrap=820)
        safety.grid(row=1, column=0, sticky="ew", padx=32, pady=(0, 16))
        for text in (
                "No telemetry or external data upload",
                "No credentials, game files or personal databases in release packages",
                "Exact build fingerprint required before Play",
                "Temporary hosts routing restored by the existing guarded launcher"):
            tk.Label(safety.body, text="•  " + text, bg=COLORS["surface"],
                     fg=COLORS["text"], font=("Segoe UI", 9)).pack(
                         anchor="w", pady=3)

        paths = self._card(page, "Local data")
        paths.grid(row=2, column=0, sticky="ew", padx=32, pady=(0, 28))
        tk.Label(paths.body, text=os.fspath(settings_path()),
                 bg=COLORS["surface"], fg=COLORS["muted"],
                 font=("Segoe UI", 9), wraplength=850, justify="left").pack(anchor="w")
        actions = tk.Frame(paths.body, bg=COLORS["surface"])
        actions.pack(fill="x", pady=(14, 0))
        ttk.Button(actions, text="CREATE DESKTOP SHORTCUT",
                   style="Secondary.TButton",
                   command=self.create_desktop_shortcut).pack(side="left")
        ttk.Button(actions, text="RESET LAUNCHER SETTINGS",
                   style="Danger.TButton", command=self.reset_launcher_settings).pack(
                       side="left", padx=(8, 0))

    def _build_guide(self, page: tk.Frame) -> None:
        page.grid_columnconfigure((0, 1), weight=1)
        page.grid_rowconfigure(1, weight=1)

        start = self._card(
            page, "Quick start",
            "Everything needed for LocalFUT stays inside this package.",
            subtitle_wrap=820)
        start.grid(row=0, column=0, columnspan=2, sticky="ew", padx=32,
                   pady=(28, 16))
        tk.Label(
            start.body,
            text=("1. Open Game Configuration and run Auto-Detect.\n"
                  "2. Confirm the exact compatible build and choose NORMAL or RTG.\n"
                  "3. Select Start Local FUT and approve Windows only when asked.\n"
                  "4. Enter FUT after the launcher reports Local Server Ready.\n"
                  "5. Use Stop Local Server before closing the session."),
            bg=COLORS["surface"], fg=COLORS["text"], font=("Segoe UI", 9),
            justify="left", anchor="w").pack(fill="x")

        builds = self._card(page, "Compatible builds")
        builds.grid(row=1, column=0, sticky="nsew", padx=(32, 10),
                    pady=(0, 28))
        tk.Label(
            builds.body,
            text=("Preferred\nFIFA 19 PC EA App — 19.0.4052077.0\n\n"
                  "Limited compatibility\nFIFA 19 PC v1.0.0.0 — 19.0.3865658.0\n\n"
                  "Only this exact v1 executable and CardsDLL pair is covered; other v1 releases are unsupported. Other builds are shown with fingerprints, but Play remains disabled because compatibility is not covered."),
            bg=COLORS["surface"], fg=COLORS["text"], font=("Segoe UI", 9),
            wraplength=390, justify="left", anchor="nw").pack(fill="both",
                                                               expand=True)

        support = self._card(page, "Accounts and troubleshooting")
        support.grid(row=1, column=1, sticky="nsew", padx=(10, 32),
                     pady=(0, 28))
        tk.Label(
            support.body,
            text=("NORMAL and RTG are separate local accounts. Use the sidebar switch before starting FIFA; switching is blocked during an active session.\n\n"
                  + ECONOMY_REFRESH_NOTE + "\n\n"
                  "After a bug, freeze or unexpected exit, do not repeat the action. Open Diagnostics, note the exact screen, action and time, then copy Game Errors or export the latest report.\n\n"
                  "Game Errors shows recorded evidence only. If no cause was captured, it says so instead of guessing."),
            bg=COLORS["surface"], fg=COLORS["text"], font=("Segoe UI", 9),
            wraplength=390, justify="left", anchor="nw").pack(fill="both",
                                                               expand=True)

    def show_page(self, name: str) -> None:
        self.pages[name].tkraise()
        self.page_title.set("Game Configuration" if name == "Game" else name)
        for key, button in self.nav_buttons.items():
            active = key == name
            button.configure(bg=COLORS["blue"] if active else COLORS["navy"],
                             fg="white" if active else "#B9CAE2")
        if name == "Diagnostics":
            self.refresh_diagnostic_view()

    def _start_task(self, label: str, function, callback=None) -> None:
        if self.task_running:
            messagebox.showinfo(APP_NAME, "Another launcher action is still running.")
            return
        self.task_running = True
        self.top_status.set(label)
        self.tool_output_status.set(label)

        def worker() -> None:
            try:
                result = function()
                self.events.put(("task", label, result, None, callback))
            except Exception as exc:
                self.events.put(("task", label, None, exc, callback))

        threading.Thread(target=worker, daemon=True).start()

    def _drain_events(self) -> None:
        try:
            while True:
                event = self.events.get_nowait()
                if event[0] == "task":
                    _kind, label, result, error, callback = event
                    self.task_running = False
                    if error is not None:
                        self.top_status.set("Action failed")
                        self.tool_output_status.set(str(error).splitlines()[-1][:180])
                        messagebox.showerror(APP_NAME, str(error))
                    else:
                        self.top_status.set("Ready")
                        text = ("%s completed." % label.rstrip(".")
                                if isinstance(result, dict) else
                                str(result or "%s completed." % label))
                        self.tool_output_status.set(text.splitlines()[-1][:180])
                        if callback:
                            callback(result)
                elif event[0] == "runtime":
                    self.status_pending = False
                    self._apply_runtime(event[1])
        except queue.Empty:
            pass
        self.after(100, self._drain_events)

    def auto_detect(self, force_all: bool = False) -> None:
        saved_paths = [str(path) for path in self.settings.get("gamePaths", [])
                       if isinstance(path, str)]
        saved_path = str(self.settings.get("gamePath", ""))
        if saved_path:
            saved_paths.append(saved_path)
        saved_profile = str(self.settings.get("profileId", ""))
        operation = lambda: detect(
            saved_paths, "" if force_all else saved_profile)
        self._start_task("Scanning FIFA 19 installations...", operation,
                         self._apply_detection)

    def check_entered_path(self) -> None:
        value = self.game_path.get().strip()
        if not value:
            messagebox.showwarning(APP_NAME, "Enter or browse to a FIFA 19 folder first.")
            return
        self._start_task(
            "Checking selected path...", lambda: detect([value], "", True),
            self._apply_detection)

    def browse_game(self) -> None:
        value = filedialog.askdirectory(
            title="Select the FIFA 19 installation folder",
            initialdir=self.game_path.get() or os.fspath(Path.home()))
        if value:
            self.game_path.set(value)
            self.check_entered_path()

    def _apply_detection(self, result: dict[str, object]) -> None:
        self._set_compatibility(result)
        self.installations = list(result.get("installations", []))
        for row in self.installations:
            if row.get("known"):
                remember_game_path(
                    self.settings, str(row.get("gameDirectory", "")))
        save_settings(self.settings)
        self.install_tree.delete(*self.install_tree.get_children())
        for index, row in enumerate(self.installations):
            display_name = str(row.get("displayName") or
                               "Unknown FIFA 19 build").replace(
                                   "FIFA 19 PC ", "").replace(" (", " ").rstrip(")")
            support = str(row.get("supportStatus") or "unsupported")
            short_support = "Beta" if support == "observed-beta" else support.title()
            self.install_tree.insert(
                "", "end", iid=str(index), values=(
                    display_name,
                    short_support,
                    row.get("gameDirectory") or "-"))
        selected = result.get("selected")
        self.selected_installation = selected if isinstance(selected, dict) else None
        if self.selected_installation:
            for index, row in enumerate(self.installations):
                if row.get("executable") == self.selected_installation.get("executable"):
                    self.install_tree.selection_set(str(index))
                    self.install_tree.focus(str(index))
                    self.install_tree.see(str(index))
                    break
            self._select_installation(self.selected_installation, persist=True)
            self.install_status.set("Exact build")
        else:
            self.install_status.set(str(result.get("decision", "Not found")).title())
            if (result.get("decision") == "unsupported" and
                    self.installations):
                self.install_tree.selection_set("0")
                self.install_tree.focus("0")
                self._select_installation(self.installations[0], persist=False)
            else:
                self.game_path.set("")
                self.build_name.set("No supported build selected")
                self.build_support.set("Unknown")
                self.build_strategy.set("-")
                self.build_fingerprint.set("-")
            self.top_status.set(str(result.get("message", "Detection finished")))

    def _tree_selected(self, _event=None) -> None:
        selected = self.install_tree.selection()
        if selected:
            self._select_installation(self.installations[int(selected[0])],
                                      persist=False)

    def use_tree_selection(self) -> None:
        selected = self.install_tree.selection()
        if not selected:
            messagebox.showwarning(APP_NAME, "Select an installation first.")
            return
        row = self.installations[int(selected[0])]
        if not row.get("known"):
            messagebox.showwarning(
                APP_NAME,
                "This FIFA 19 version may work, but FUT Deba does not cover its compatibility. Play remains disabled until an exact compatible build is selected.")
            return
        self._select_installation(row, persist=True)
        messagebox.showinfo(APP_NAME, "The selected FIFA 19 installation was saved.")

    def _select_installation(self, row: dict[str, object], persist: bool) -> None:
        self.selected_installation = row if row.get("known") else None
        self._set_compatibility({
            "decision": "selected" if row.get("known") else "unsupported",
            "selected": row if row.get("known") else None,
        })
        self.game_path.set(str(row.get("gameDirectory", "")))
        self.build_name.set(str(row.get("displayName") or "Unknown FIFA 19 build"))
        self.build_support.set(str(row.get("supportStatus") or "unsupported"))
        self.build_strategy.set(str(row.get("launchStrategy") or "-"))
        fingerprints = list(row.get("fingerprints", []))
        self.build_fingerprint.set("\n".join(
            "%s · %s · v%s · %s…" % (
                item.get("relativePath", "file"),
                format_bytes(int(item.get("sizeBytes", 0))),
                str(item.get("productVersion") or item.get("fileVersion") or "unknown"),
                str(item.get("sha256") or "unknown")[:8])
            for item in fingerprints) or str(row.get("reason", "-")))
        if persist and self.selected_installation:
            self.settings["gamePath"] = str(row["gameDirectory"])
            self.settings["profileId"] = str(row["profileId"])
            remember_game_path(self.settings, str(row["gameDirectory"]))
            save_settings(self.settings)
        self._schedule_status_refresh()

    def copy_build_report(self) -> None:
        selected = self.install_tree.selection()
        row = (self.installations[int(selected[0])] if selected else
               self.selected_installation)
        if not row:
            messagebox.showinfo(APP_NAME, "No build report is available.")
            return
        report = json.dumps({
            "displayName": row.get("displayName"),
            "supportStatus": row.get("supportStatus"),
            "launchStrategy": row.get("launchStrategy"),
            "gameDirectory": row.get("gameDirectory"),
            "fingerprints": row.get("fingerprints", []),
        }, indent=2)
        self.clipboard_clear()
        self.clipboard_append(report)
        self.update()
        self.top_status.set("Build report copied")

    def _schedule_status_refresh(self) -> None:
        if not self.status_pending:
            self.status_pending = True
            root = profile_directory(self.selected_installation,
                                     self.account_mode.get())

            def worker() -> None:
                self.events.put(("runtime", runtime_snapshot(root)))

            threading.Thread(target=worker, daemon=True).start()
        if self.status_timer is None:
            self.status_timer = self.after(3500, self._status_timer_tick)

    def _status_timer_tick(self) -> None:
        self.status_timer = None
        self._schedule_status_refresh()

    def _apply_runtime(self, value: dict[str, object]) -> None:
        self.runtime = value
        server_ready = bool(value.get("serverReady"))
        fifa_running = bool(value.get("fifaRunning"))
        if (close_request_ignored(self.launch_action, fifa_running,
                                  self.launch_requested_at, time.monotonic())
                and not getattr(self, "force_close_offered", False)):
            self.force_close_offered = True
            self._offer_force_close()
        if (self.launch_action in {"stopping", "closing"} and
                not server_ready and not fifa_running):
            self.launch_action = ""
        elif self.launch_action == "starting" and server_ready and fifa_running:
            if getattr(self, "ready_seen_at", None) is None:
                self.ready_seen_at = time.monotonic()
            if game_settled(self.selected_installation, self.ready_seen_at,
                            time.monotonic()):
                self.launch_action = ""
                self.ready_seen_at = None
        elif self.launch_action == "starting" and not server_ready:
            reason = start_failure(
                local_data_root() / "launcher-action.log",
                getattr(self, "action_log_offset", 0))
            timed_out = (not fifa_running and self.launch_requested_at and
                         time.monotonic() - self.launch_requested_at > 120)
            if reason or timed_out:
                self.launch_action = "failed"
                self.start_failure_reason = reason
            elif should_offer_manual_continue(
                    str((self.selected_installation or {}).get(
                        "launchStrategy") or ""),
                    self.launch_action,
                    (time.monotonic() - self.launch_requested_at)
                    if self.launch_requested_at else 0.0,
                    fifa_running, server_ready,
                    getattr(self, "manual_continue_offered", False)):
                self.manual_continue_offered = True
                self._offer_manual_continue()
        self.server_status.set("Ready" if server_ready else "Offline")
        self.game_status.set("Running" if fifa_running else "Closed")
        latest = value.get("latestDiagnostic")
        self.session_status.set(
            datetime.fromtimestamp(Path(latest).stat().st_mtime).strftime("%d %b %H:%M")
            if latest else "No diagnostics")
        self.diagnostics_path.set(os.fspath(latest) if latest else
                                  os.fspath(value.get("diagnostics", "-")))
        self.diagnostics_summary.set(str(value.get("diagnosticSummary", "")))
        visual = session_visual_state(
            self.selected_installation is not None, server_ready,
            fifa_running, self.launch_action)
        if (self.launch_action == "failed" and
                getattr(self, "start_failure_reason", None)):
            visual = dict(visual, message=(
                "LocalFUT could not start: %s" % self.start_failure_reason))
        self._show_session_state(visual)
        self._paint_status_card(
            "installation", "success" if self.selected_installation else
            "warning")
        self._paint_status_card(
            "local server", "success" if server_ready else
            ("progress" if self.launch_action == "starting" else "neutral"))
        self._paint_status_card(
            "game process", "success" if fifa_running else
            ("progress" if self.launch_action == "starting" else "neutral"))
        summary = self.diagnostics_summary.get().lower()
        session_tone = ("success" if ("closed cleanly" in summary or
                                      "ready state" in summary) else
                        "danger" if "error" in summary and
                        "no explicit error" not in summary else "neutral")
        self._paint_status_card("last session", session_tone)
        self.top_status.set({
            "success": "LOCAL SESSION READY",
            "progress": "LOCAL FUT IS STARTING",
            "warning": "ACTION REQUIRED",
            "danger": "CHECK DIAGNOSTICS",
            "neutral": "Ready to start",
        }[str(visual["tone"])])

    def refresh_everything(self) -> None:
        self.auto_detect(force_all=True)
        self._schedule_status_refresh()

    def _tool_environment(self) -> dict[str, str]:
        environment = dict(os.environ)
        mode = self.account_mode.get().upper()
        environment["LOCALFUT19_PROFILE"] = mode
        environment["LOCALFUT19_RTG_MODE"] = "1" if mode == "RTG" else "0"
        environment["LOCALFUT19_DATA_ROOT"] = os.fspath(profile_directory(
            self.selected_installation, mode))
        return environment

    def _ensure_tools_safe(self, destructive: bool) -> None:
        profile = server_profile()
        if profile and str(profile.get("profileMode", "NORMAL")).upper() != self.account_mode.get():
            raise RuntimeError("The active server uses a different account profile.")
        if destructive and (server_health() or fifa_process_running()):
            raise RuntimeError("Close FIFA 19 and stop LocalFUT before this operation.")

    def run_account_tool(self, command: str, *arguments: str,
                         destructive: bool = False) -> None:
        def operation() -> str:
            self._ensure_tools_safe(destructive)
            return run_hidden([
                os.fspath(python_executable()), "-B",
                os.fspath(SERVER_DIR / "fut_tools.py"), command, *arguments,
            ], self._tool_environment())

        self._start_task(
            "Running %s..." % command, operation,
            lambda output: messagebox.showinfo(
                APP_NAME, format_account_info(str(output))
                if command == "info" else
                account_tool_message(command, output)))

    def _add_positive(self, command: str, raw_value: str) -> None:
        try:
            value = int(raw_value.strip())
            if value < 1 or value > 2_147_483_647:
                raise ValueError
        except ValueError:
            messagebox.showerror(APP_NAME, "Enter a positive whole number up to 2,147,483,647.")
            return
        self.run_account_tool(command, str(value))

    def save_draft_quality(self) -> None:
        preset = self.draft_quality.get()
        self._start_task(
            "Saving Draft quality...",
            lambda: run_hidden([
                os.fspath(python_executable()), "-B",
                os.fspath(SERVER_DIR / "fut_draft_config.py"), "set", preset]),
            lambda output: messagebox.showinfo(APP_NAME, str(output)))

    def confirm_reset(self, kind: str) -> None:
        descriptions = {
            "draft": (
                "Discard the unfinished Single Player Draft and refund its entry.\n\n"
                "Preserved: club, other modes, finished Draft history and claimed rewards."),
            "sbc": (
                "Clear completed SBC progress so the selected set can be played again.\n\n"
                "Preserved: cards, claimed rewards, coins and every other account area."),
            "club": (
                "Create a backup, delete inventory items and squads, then restore the starter club.\n\n"
                "Preserved: coins, record, packs, objectives, histories, SBC progress and onboarding."),
            "account": (
                "Create a backup and reset coins, inventory, squads, packs, transfers, objectives, matches, record, SBC state and onboarding.\n\n"
                "The first-time FUT setup will return."),
        }
        if kind == "account":
            answer = simpledialog.askstring(
                "Full account reset", descriptions[kind] +
                "\n\nType RESET to continue:", parent=self)
            if answer != "RESET":
                return
        elif not messagebox.askyesno("Confirm reset", descriptions[kind] +
                                     "\n\nContinue?", parent=self):
            return
        if kind == "draft":
            self.run_account_tool("resetdraft", destructive=True)
        elif kind == "sbc":
            value = self.sbc_set_id.get().strip()
            if value and not value.isdigit():
                messagebox.showerror(APP_NAME, "SBC set id must be numeric or empty for all sets.")
                return
            self.run_account_tool("resetsbc", *(value,) if value else (),
                                  destructive=True)
        elif kind == "club":
            self.run_account_tool("reset", destructive=True)
        else:
            self.run_account_tool("resetaccount", destructive=True)

    def confirm_season_final_test(self) -> None:
        mode=self.account_mode.get().upper()
        answer=simpledialog.askstring(
            "Prepare Season final test",
            "This creates a verified database backup, then advances only the "
            "entered offline Season in the %s profile to round 14 of 15.\n\n"
            "Preserved: club, coins, packs, Picks, SBCs, Draft, objectives, "
            "market and every other mode. No synthetic match reward is "
            "granted.\n\nType SEASON 15 to continue:" % mode,
            parent=self)
        if answer != "SEASON 15":
            return
        self.run_account_tool("prepareseasonfinal", destructive=True)

    def start_local_fut(self) -> None:
        if not self.selected_installation:
            messagebox.showerror(
                APP_NAME,
                "Select one of the two exact compatible FIFA 19 builds first. Other versions may work, but their compatibility is not covered.")
            self.show_page("Game")
            return
        if server_health() or fifa_process_running():
            messagebox.showwarning(APP_NAME, "FIFA 19 or the local server is already active.")
            return
        # The 1.0.0.0 path connects only on one specific installation. Testers
        # on other 1.0.0.0 copies reached "Unable to connect" with nothing
        # said up front (2026-09-14), so warn before the session starts.
        if (str(self.selected_installation.get("launchStrategy") or "")
                == "legacy-native-server"):
            if not messagebox.askyesno(
                    APP_NAME,
                    "This is the FIFA 19 1.0.0.0 build. FUT Deba fully "
                    "supports only the EA App build (19.0.4052077.0). On "
                    "1.0.0.0 the local session works only with one specific "
                    "installation and may fail with \"Unable to connect\" on "
                    "others.\n\nStart anyway?", parent=self):
                return
        self.launch_action = "starting"
        self.launch_requested_at = time.monotonic()
        self.ready_seen_at = None
        self.start_failure_reason = None
        self.manual_continue_offered = False
        self.action_log_offset = action_log_size(
            local_data_root() / "launcher-action.log")
        self._show_session_state(session_visual_state(
            True, False, False, self.launch_action))
        self.top_status.set("APPROVE THE WINDOWS PROMPT")
        self.update_idletasks()
        log_path = local_data_root() / "launcher-action.log"
        try:
            elevated_action("Start", log_path, self.selected_installation,
                            self.account_mode.get())
        except RuntimeError as exc:
            self.launch_action = ""
            self._show_session_state(session_visual_state(
                True, False, False, self.launch_action))
            self.deiconify()
            self.lift()
            self.focus_force()
            messagebox.showwarning(APP_NAME, str(exc), parent=self)
            return
        self.top_status.set("LOCAL FUT IS STARTING")
        self.tool_output_status.set("Protected launch requested. Monitoring local status...")
        self.after(1800, self._schedule_status_refresh)

    def stop_local_fut(self) -> None:
        if (not self.launch_action and not server_health() and
                not fifa_process_running()):
            messagebox.showinfo(APP_NAME, "No local FUT session is active.")
            return
        if fifa_process_running():
            if not messagebox.askyesno(
                    APP_NAME,
                    "Close FIFA 19 and end the local session?\n\n"
                    "Finish or leave any active match first. LocalFUT will restore "
                    "Windows routing after the game closes."):
                return
            # The protected launch starts FIFA 19 elevated, so this launcher
            # can neither deliver a close request to it nor end it: live v1
            # 2026-09-13 showed both dialogs. The elevated runner can, and
            # this stop request tells it to close the game.
            stop_file = stop_request_path(
                self.selected_installation, self.account_mode.get())
            try:
                stop_file.parent.mkdir(parents=True, exist_ok=True)
                stop_file.write_text(
                    datetime.now().astimezone().isoformat(), encoding="utf-8")
            except OSError:
                pass
            request_fifa_close()
            self.force_close_offered = False
            self.launch_action = "closing"
            self.launch_requested_at = time.monotonic()
            self._show_session_state(session_visual_state(
                self.selected_installation is not None, True, True,
                self.launch_action))
            self.top_status.set("CLOSING FIFA 19")
            self.tool_output_status.set(
                "Waiting for FIFA 19 to close before LocalFUT restores routing...")
            self.after(500, self._schedule_status_refresh)
            return
        stop_file = stop_request_path(
            self.selected_installation, self.account_mode.get())
        try:
            stop_file.parent.mkdir(parents=True, exist_ok=True)
            stop_file.write_text(
                datetime.now().astimezone().isoformat(), encoding="utf-8")
        except OSError as exc:
            messagebox.showerror(APP_NAME, "The stop request could not be written:\n%s" % exc)
            return
        self.launch_action = "stopping"
        self.launch_requested_at = time.monotonic()
        self._show_session_state(session_visual_state(
            self.selected_installation is not None,
            bool(self.runtime.get("serverReady")),
            bool(self.runtime.get("fifaRunning")), self.launch_action))
        self.top_status.set("STOPPING LOCAL FUT")
        self.tool_output_status.set("The existing launcher will restore routing before it exits.")
        self.after(500, self._schedule_status_refresh)

    def _offer_manual_continue(self) -> None:
        if not messagebox.askyesno(
                APP_NAME,
                "FIFA 19 is taking longer than usual to be detected "
                "automatically.\n\nIf FIFA 19 is already at its MAIN MENU, "
                "FUT Deba can connect it now. Is FIFA 19 at its main menu?",
                parent=self):
            return
        try:
            write_manual_continue_signal()
        except OSError as exc:
            messagebox.showwarning(APP_NAME, str(exc), parent=self)
            return
        messagebox.showinfo(
            APP_NAME,
            "Connecting to FIFA 19. When the launcher says you can enter FUT, "
            "if FIFA shows \"Press ... to reconnect\", press that so the game "
            "reaches the local server.", parent=self)

    def _offer_force_close(self) -> bool:
        if not messagebox.askyesno(
                APP_NAME,
                "FIFA 19 did not close after the normal close request.\n\n"
                "Force it to close now? Progress in an unfinished match is lost. "
                "LocalFUT restores Windows routing once the game has exited."):
            return False
        if force_close_fifa():
            return True
        messagebox.showerror(
            APP_NAME, "FIFA 19 could not be closed. End it from Task Manager, "
            "then press STOP LOCAL FUT again.")
        return False

    def remove_hosts(self) -> None:
        if server_health() or fifa_process_running():
            messagebox.showerror(APP_NAME, "Close FIFA 19 and stop LocalFUT before cleaning hosts.")
            return
        if not messagebox.askyesno(
                "Remove LocalFUT routing",
                "Remove only LocalFUT markers and redirect lines from the Windows hosts file.\n\n"
                "Unrelated hosts entries are preserved. Stale LocalFUT recovery snapshots are removed.\n\n"
                "Windows will request administrator approval. Continue?", parent=self):
            return
        try:
            elevated_action("RemoveHosts", local_data_root() / "launcher-action.log")
        except RuntimeError as exc:
            messagebox.showerror(APP_NAME, str(exc))
            return
        self.top_status.set("Hosts cleanup requested")

    def _offer_set_aside_return(self) -> None:
        lines = set_aside_lines()
        if not lines or server_health() or fifa_process_running():
            return
        if not messagebox.askyesno(
                APP_NAME,
                "A FUT Deba session did not end normally, so these redirect "
                "lines of another FUT tool are still set aside:\n\n" +
                "\n".join(lines[:6]) +
                "\n\nPut them back now? Windows will ask for administrator "
                "approval. Otherwise they go back when your next session "
                "ends.", parent=self):
            return
        try:
            elevated_action(
                "RemoveHosts", local_data_root() / "launcher-action.log")
        except RuntimeError as exc:
            messagebox.showwarning(APP_NAME, str(exc), parent=self)

    def _diagnostics_directory(self) -> Path:
        return profile_directory(self.selected_installation,
                                 self.account_mode.get()) / "diagnostics"

    def open_diagnostics(self) -> None:
        directory = self._diagnostics_directory()
        directory.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(directory)  # type: ignore[attr-defined]
        except OSError as exc:
            messagebox.showerror(APP_NAME, str(exc))

    def refresh_diagnostic_view(self) -> None:
        latest = latest_diagnostic_path(self._diagnostics_directory())
        self.diagnostics_path.set(os.fspath(latest) if latest else
                                  os.fspath(self._diagnostics_directory()))
        self.diagnostics_summary.set(diagnostic_summary(latest))
        text = read_diagnostic(latest) if latest else "No diagnostic report is available."
        lines = text.splitlines()[-220:]
        self.diagnostic_text.configure(state="normal")
        self.diagnostic_text.delete("1.0", "end")
        self.diagnostic_text.insert("1.0", "\n".join(lines))
        self.diagnostic_text.configure(state="disabled")
        error_entries = game_error_entries(self._diagnostics_directory())
        error_report = ("\n\n".join(error_entries) if error_entries else
                        "No recorded game errors were found in the latest session.")
        self.game_error_summary.set(
            "%d recorded game error event%s in the latest session." % (
                len(error_entries), "" if len(error_entries) == 1 else "s")
            if error_entries else "No recorded game errors in the latest session.")
        self.game_error_summary_label.configure(
            fg=COLORS["danger"] if error_entries else COLORS["success"])
        self.game_error_text.configure(state="normal")
        self.game_error_text.delete("1.0", "end")
        self.game_error_text.insert("1.0", error_report)
        self.game_error_text.configure(state="disabled")

    def copy_diagnostic_summary(self) -> None:
        selected = self.selected_installation or {}
        report = "\n".join((
            "FUT Deba Launcher %s" % APP_VERSION,
            "Build: %s" % (selected.get("displayName") or "not selected"),
            "Game: %s" % (selected.get("executable") or "not selected"),
            "Account: %s" % self.account_mode.get(),
            "Server: %s" % self.server_status.get(),
            "Game errors: %s" % self.game_error_summary.get(),
            "Diagnostic: %s" % self.diagnostics_path.get(),
            "Summary: %s" % self.diagnostics_summary.get(),
        ))
        self.clipboard_clear()
        self.clipboard_append(report)
        self.update()
        self.top_status.set("Diagnostic summary copied")

    def copy_game_errors(self) -> None:
        report = game_error_report(self._diagnostics_directory())
        self.clipboard_clear()
        self.clipboard_append(report)
        self.update()
        self.top_status.set("Game error evidence copied")

    def export_latest_diagnostic(self) -> None:
        latest = latest_diagnostic_path(self._diagnostics_directory())
        if latest is None:
            messagebox.showinfo(APP_NAME, "No diagnostic report is available.")
            return
        destination = filedialog.asksaveasfilename(
            title="Export latest diagnostic", initialfile=latest.name,
            defaultextension=latest.suffix)
        if destination:
            try:
                shutil.copy2(latest, destination)
            except OSError as exc:
                messagebox.showerror(APP_NAME, str(exc))
            else:
                messagebox.showinfo(APP_NAME, "Diagnostic exported successfully.")

    def _account_mode_changed(self) -> None:
        previous = str(self.settings.get("accountMode", "NORMAL"))
        requested = self.account_mode.get()
        if requested != previous and (server_health() or fifa_process_running()):
            self.account_mode.set(previous)
            messagebox.showerror(
                APP_NAME,
                "Stop FIFA 19 and LocalFUT before switching the account profile.")
            return
        self.settings["accountMode"] = requested
        self.profile_status.set("ACCOUNT: %s" % requested)
        self.account_switch_label.set(
            "SWITCH TO %s" % ("RTG" if requested == "NORMAL" else "NORMAL"))
        self._schedule_status_refresh()
        if hasattr(self, "diagnostic_text"):
            self.refresh_diagnostic_view()

    def _toggle_account_mode(self) -> None:
        self.account_mode.set(
            "RTG" if self.account_mode.get() == "NORMAL" else "NORMAL")
        self._account_mode_changed()

    def create_desktop_shortcut(self) -> None:
        script = TOOLS_DIR / "futdeba_launcher_action.ps1"

        def operation() -> str:
            return run_hidden([
                "powershell.exe", "-NoProfile", "-WindowStyle", "Hidden",
                "-ExecutionPolicy", "Bypass", "-File", os.fspath(script),
                "-Action", "CreateShortcut", "-LogPath",
                os.fspath(local_data_root() / "launcher-action.log"),
            ])

        self._start_task(
            "Creating desktop shortcut...", operation,
            lambda _output: messagebox.showinfo(
                APP_NAME, "The FUT Deba desktop shortcut was created."))

    def _save_preferences(self) -> None:
        self.settings["confirmClose"] = self.confirm_close.get()
        save_settings(self.settings)

    def reset_launcher_settings(self) -> None:
        if not messagebox.askyesno(
                APP_NAME,
                "Reset only the saved launcher game selection and preferences?\n\n"
                "Game files, LocalFUT accounts, logs and Windows hosts are preserved."):
            return
        self.settings = default_settings()
        save_settings(self.settings)
        self.account_mode.set("NORMAL")
        self._account_mode_changed()
        self.confirm_close.set(True)
        self.game_path.set("")
        self.auto_detect(force_all=True)

    def _close(self) -> None:
        if self.confirm_close.get() and server_health():
            answer = messagebox.askyesnocancel(
                APP_NAME,
                "The local server is active. Request a safe stop before closing the launcher?")
            if answer is None:
                return
            if answer:
                self.stop_local_fut()
        self.destroy()


def check_environment() -> int:
    result = detect()
    payload = {
        "launcher": APP_VERSION,
        "tk": tk.TkVersion,
        "decision": result["decision"],
        "selected": (result.get("selected") or {}).get("displayName"),
        "assets": {name: (ASSET_DIR / filename).is_file() for name, filename in (
            ("hero", "futdeba-hero-v2.png"),
            ("profile", "futdeba-brand-v2.png"),
            ("iconPng", "launcher-icon-v3.png"),
            ("iconIco", "launcher-icon-v3.ico"),
        )},
    }
    print(json.dumps(payload, indent=2))
    return 0 if result["decision"] in {"selected", "not-found"} and all(
        payload["assets"].values()) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="validate dependencies, assets and game detection without opening the UI")
    parser.add_argument("--account-mode", choices=("NORMAL", "RTG"),
                        help="select the account shown when the launcher opens")
    args = parser.parse_args(argv)
    if args.check:
        return check_environment()
    instance = acquire_launcher_instance()
    if instance is None:
        ctypes.windll.user32.MessageBoxW(
            0, "FUT Deba Launcher is already open. Use the existing window.",
            APP_NAME, 0x40)
        return 0
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        pass
    try:
        app = FutDebaLauncher(args.account_mode)
        app.mainloop()
    finally:
        release_launcher_instance(instance)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
