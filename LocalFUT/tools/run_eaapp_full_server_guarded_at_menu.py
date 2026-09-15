#!/usr/bin/env python3
"""Run the full local network core with only fingerprinted EA App guards."""

from __future__ import annotations

import argparse
import csv
import ctypes
import json
import os
import re as _re
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS_DIR = os.path.join(ROOT, "tools")
if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)

from guard_eaapp_origin_eligibility_missing_text import (
    agent_source as eligibility_guard_agent_source,
)
from guard_eaapp_origin_session_missing_values import (
    agent_source as origin_session_guard_agent_source,
)
from guard_eaapp_fut_champions_registration import (
    agent_source as fut_champions_registration_guard_agent_source,
)
from guard_eaapp_fut_champions_cpu_match import (
    agent_source as fut_champions_cpu_match_guard_agent_source,
)
from guard_eaapp_player_skill_moves import (
    agent_source as player_skill_moves_guard_agent_source,
)
from champions_offline_entry import (
    handle_offline_entry_request,
    handle_offline_match_request,
)
from observe_eaapp_pack_opening_stall import (
    agent_source as pack_opening_observer_agent_source,
)
from observe_eaapp_fut_champions_actions import (
    agent_source as fut_champions_action_observer_agent_source,
)
from observe_eaapp_fut_champions_match_transition import (
    agent_source as fut_champions_match_transition_observer_agent_source,
)
from observe_eaapp_sbc_market_return import (
    agent_source as sbc_market_return_observer_agent_source,
)
from observe_eaapp_offline_select_away import (
    agent_source as offline_select_away_observer_agent_source,
)
from observe_eaapp_season_event_cards import (
    agent_source as season_event_card_observer_agent_source,
)
from observe_eaapp_season_selection import (
    agent_source as season_selection_observer_agent_source,
)
from observe_eaapp_season_list_request import (
    agent_source as season_list_request_observer_agent_source,
)
from probe_eaapp_certificate_signature import verify_target
from probe_eaapp_local_certificate_at_menu import (
    hosts_path,
    install_temporary_hosts,
    restore_hosts,
)
from probe_eaapp_local_certificate_success_at_menu import (
    active_agent_source as certificate_guard_agent_source,
)
from probe_eaapp_local_certificate_return import emit as _emit_json
from probe_eaapp_local_certificate_return import HOSTNAME
from beta_readiness import audit as audit_beta_runtime


SERVICE_PORTS = (42230, 10051, 8199)
ATTACH_TIMEOUT_SECONDS = 15.0
EXPERIMENTAL_CHAMPIONS_ENABLED=(str(os.environ.get(
    "LOCALFUT19_ENABLE_EXPERIMENTAL_CHAMPIONS","") or "").strip().lower()
    in {"1","true","yes","on"})


def _force_console_redraw(title: str | None = None) -> None:
    """Flush and repaint the classic Windows console without user input."""
    if os.name != "nt":
        return
    try:
        if title:
            ctypes.windll.kernel32.SetConsoleTitleW(title)
        window = ctypes.windll.kernel32.GetConsoleWindow()
        if window:
            ctypes.windll.user32.RedrawWindow(
                window,
                None,
                None,
                0x0585,
            )
    except (AttributeError, OSError):
        pass


def emit(event: str, **fields: object) -> None:
    _emit_json(event, **fields)
    _force_console_redraw()


# Live v1 2026-09-13 showed that an unelevated launcher cannot close an
# elevated FIFA 19, and this bridge runs elevated as well. It closes the game
# on the launcher's stop request: a close request first, then an ended process
# if the game has not exited in time.
GAME_CLOSE_TIMEOUT_SECONDS = 20.0


def close_game(pid: int, exited: threading.Event) -> None:
    """Ask FIFA 19 to close, and end it if it has not exited in time."""
    subprocess.run(["taskkill.exe", "/PID", str(pid)],
                   capture_output=True, check=False)
    if not exited.wait(GAME_CLOSE_TIMEOUT_SECONDS):
        subprocess.run(["taskkill.exe", "/F", "/PID", str(pid)],
                       capture_output=True, check=False)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--network-log", required=True)
    parser.add_argument("--stop-file", required=True)
    parser.add_argument("--manual-continue", action="store_true")
    parser.add_argument(
        "--account-mode",
        choices=("NORMAL", "RTG"),
        default="NORMAL",
    )
    return parser.parse_args()


# Only a process whose command line names one of these is ever terminated.
# A leftover server holds the ports and blocks the next launch, but "some
# python.exe owns the port" is not enough to justify killing it: the user may
# be running something of their own.
LOCALFUT_PROCESS_MARKERS = (
    "serve_localfut19_network_only.py",
    "local_fut19_server.py",
    "run_eaapp_full_server_guarded_at_menu.py",
)


def _service_port_owners() -> dict[int, int]:
    """Return {port: pid} for the service ports currently being listened on."""
    result = subprocess.run(
        ["netstat", "-ano", "-p", "TCP"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        errors="replace",
    )
    owners: dict[int, int] = {}
    for raw_line in result.stdout.splitlines():
        columns = raw_line.split()
        if len(columns) < 5 or columns[0].upper() != "TCP":
            continue
        if columns[3].upper() != "LISTENING":
            continue
        local = columns[1].rsplit(":", 1)
        if len(local) != 2 or not local[1].isdigit():
            continue
        port = int(local[1])
        if port not in SERVICE_PORTS:
            continue
        try:
            owners[port] = int(columns[-1])
        except ValueError:
            continue
    return owners


def _process_command_line(pid: int) -> str:
    result = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
         "-Command",
         "(Get-CimInstance Win32_Process -Filter 'ProcessId=%d')"
         ".CommandLine" % int(pid)],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        errors="replace",
    )
    return (result.stdout or "").strip()


def _is_localfut_server(pid: int) -> bool:
    """True only when this pid is one of our own server processes."""
    if int(pid) in (0, os.getpid()):
        return False
    command = _process_command_line(pid).lower()
    if not command:
        return False
    return any(marker.lower() in command for marker in
               LOCALFUT_PROCESS_MARKERS)


def _process_image(pid: int) -> str:
    """The program name Windows lists for a process, or an empty string."""
    result = subprocess.run(
        ["tasklist", "/FI", "PID eq %d" % int(pid), "/FO", "CSV", "/NH"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        errors="replace",
    )
    for row in csv.reader(result.stdout.splitlines()):
        if len(row) > 1 and row[1] == str(int(pid)):
            return row[0]
    return ""


def foreign_port_holders() -> list[str]:
    """Name every program other than LocalFUT19 that holds a service port.

    Other FUT revival tools listen on the same ports. The EA App tester of
    2026-09-14, who also runs revival tools for other FIFAs, was only told
    that a previous LocalFUT19 session was holding them.
    """
    ports_by_pid: dict[int, list[int]] = {}
    for port, pid in sorted(_service_port_owners().items()):
        ports_by_pid.setdefault(pid, []).append(port)
    return [
        "%s (PID %d, port%s %s)" % (
            _process_image(pid) or "an unknown program", pid,
            "s" if len(ports) > 1 else "",
            ", ".join(str(port) for port in ports))
        for pid, ports in ports_by_pid.items()
        if not _is_localfut_server(pid)
    ]


def describe_foreign_port_holders(holders: list[str]) -> str:
    return (
        "FUT Deba cannot share its local ports with %s. This is usually "
        "another FUT revival tool: close it completely, or restart the PC, "
        "then press START LOCAL FUT again." % "; ".join(holders)
    )


def _clear_stale_localfut_servers() -> list[int]:
    """End a previous session still holding the ports, and report what it ended.

    A session that is killed instead of stopped leaves its server running.
    The next launch then refused to start, and the user was left to find a
    stray python.exe in Task Manager. Ending our own leftover is safe and is
    the only thing standing between them and a working launch.
    """
    cleared: list[int] = []
    for pid in sorted(set(_service_port_owners().values())):
        if not _is_localfut_server(pid):
            emit("stale-server-left-alone", pid=pid, reason="not ours")
            continue
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       check=False,
                       stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
        cleared.append(pid)
        emit("stale-server-ended", pid=pid)
    if cleared:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and _service_port_owners():
            time.sleep(0.2)
    return cleared


def _describe_stranded_hosts(path: object, error: BaseException) -> str:
    """Say that the redirect survived the session, and how to undo it.

    Re-raising the write error alone printed a bare
    ``PermissionError: [Errno 13]`` traceback on the 2026-09-07 tester
    shutdown. That hides the only fact the user needs: EA name resolution is
    still pointed at this machine until the entry is removed.
    """
    return (
        "the temporary EA redirect is still installed in %s and could not be "
        "removed (%s). EA connections from this machine will keep resolving "
        "to it. Run ADVANCED\\CLEANUP_OLD_REDIRECTS.cmd, approve the "
        "administrator prompt, then start again."
        % (path, error)
    )


def _describe_server_exit(code: int) -> str:
    """Turn the server's exit code into something the user can act on.

    A tester saw only "network-only server exited with code 4" and had no way
    to know that meant a previous session was still holding the ports.
    """
    if int(code) == 4:
        others = foreign_port_holders()
        if others:
            return describe_foreign_port_holders(others)
        holders = []
        for port in SERVICE_PORTS:
            probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                probe.settimeout(0.2)
                if probe.connect_ex(("127.0.0.1", port)) == 0:
                    holders.append(str(port))
            finally:
                probe.close()
        detail = (" Ports still in use: %s." % ", ".join(holders)
                  if holders else "")
        return (
            "a previous LocalFUT19 session is still running and is holding "
            "the local ports.%s Close every FIFA 19 and LocalFUT19 window, "
            "wait a few seconds and start again. If nothing is open, end any "
            "leftover python.exe in Task Manager, or sign out and back in."
            % detail
        )
    return "the local server exited with code %s before it was ready." % code


def _wait_for_ports(process: subprocess.Popen[str], timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    pending = set(SERVICE_PORTS)
    while pending and time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                _describe_server_exit(process.returncode)
            )
        for port in tuple(pending):
            probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                probe.settimeout(0.2)
                if probe.connect_ex(("127.0.0.1", port)) == 0:
                    pending.remove(port)
            finally:
                probe.close()
        if pending:
            time.sleep(0.1)
    if pending:
        raise RuntimeError(
            "network-only server did not bind ports %s"
            % ", ".join(str(port) for port in sorted(pending))
        )


def _remote_redirector_connections(pid: int) -> list[str]:
    """Return remote TCP 42230 endpoints currently owned by FIFA."""
    result = subprocess.run(
        ["netstat", "-ano", "-p", "TCP"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        errors="replace",
    )
    matches: list[str] = []
    for raw_line in result.stdout.splitlines():
        columns = raw_line.split()
        if len(columns) < 5 or columns[0].upper() != "TCP":
            continue
        try:
            owner_pid = int(columns[-1])
        except ValueError:
            continue
        remote = columns[2]
        if owner_pid != pid or not remote.endswith(":42230"):
            continue
        address = remote.rsplit(":", 1)[0].strip("[]").lower()
        if address not in ("127.0.0.1", "::1"):
            matches.append(remote)
    return matches


def redirector_endpoint_guard_agent_source() -> str:
    """Redirect FIFA's cached IPv4 redirector endpoint to loopback.

    This guard is loaded only after the user explicitly confirms the manual
    main-menu continuation. Normal automatic starts still rely solely on the
    temporary hosts entry. The local server is already listening and the
    certificate guard is already armed before this script is loaded.
    """
    return r"""
'use strict';
const ws2 = Process.findModuleByName('ws2_32.dll');
if (ws2 === null) {
  send({ event: 'refused', reason: 'ws2_32.dll is not loaded' });
} else {
  const attached = [];
  const seenAddresses = {};
  let rewriteCount = 0;

  function exportAddress(name) {
    try { return ws2.getExportByName(name); } catch (_) { return null; }
  }

  function attachConnect(name, label) {
    const target = exportAddress(name);
    const key = target === null ? null : target.toString();
    if (key === null || seenAddresses[key]) return;
    seenAddresses[key] = true;
    Interceptor.attach(target, {
      onEnter(args) {
        const sockaddr = args[1];
        let length = 0;
        try { length = args[2].toInt32(); } catch (_) { return; }
        if (sockaddr.isNull() || length < 8) return;
        try {
          const family = sockaddr.readU8() | (sockaddr.add(1).readU8() << 8);
          const port = (sockaddr.add(2).readU8() << 8) |
            sockaddr.add(3).readU8();
          if (family !== 2 || port !== 42230) return;
          const octets = [];
          for (let index = 0; index < 4; index++)
            octets.push(sockaddr.add(4 + index).readU8());
          if (octets[0] === 127 && octets[1] === 0 &&
              octets[2] === 0 && octets[3] === 1) return;
          sockaddr.add(4).writeByteArray([127, 0, 0, 1]);
          rewriteCount++;
          send({ event: 'redirector-endpoint-rewritten', api: label,
            original: octets.join('.') + ':42230',
            replacement: '127.0.0.1:42230', count: rewriteCount });
        } catch (error) {
          send({ event: 'redirector-endpoint-rewrite-failed', api: label,
            reason: String(error) });
        }
      }
    });
    attached.push(label);
  }

  [['connect', 'connect'], ['WSAConnect', 'WSAConnect']].forEach(function (row) {
    attachConnect(row[0], row[1]);
  });
  if (attached.length === 0) {
    send({ event: 'refused', reason: 'connect exports are unavailable' });
  } else {
    send({ event: 'armed', apis: attached, port: 42230,
      replacement: '127.0.0.1' });
  }
}
"""


def _attach_with_retry(device: object, pid: int) -> object:
    deadline = time.monotonic() + ATTACH_TIMEOUT_SECONDS
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        remote = _remote_redirector_connections(pid)
        if remote:
            raise RuntimeError(
                "FIFA contacted the remote redirector before the local bridge "
                "was armed; close FIFA and retry this updated launcher"
            )
        try:
            return device.attach(pid)
        except Exception as error:
            last_error = error
            time.sleep(0.25)
    error_name = type(last_error).__name__ if last_error else "unknown"
    raise RuntimeError(
        "could not attach the guarded bridge to FIFA within %.1f seconds (%s)"
        % (ATTACH_TIMEOUT_SECONDS, error_name)
    ) from last_error


def _verify_local_redirector_resolution() -> list[str]:
    addresses = sorted(
        {
            row[4][0]
            for row in socket.getaddrinfo(
                HOSTNAME,
                42230,
                family=socket.AF_INET,
                type=socket.SOCK_STREAM,
            )
        }
    )
    if not addresses or any(
        address not in ("127.0.0.1", "::1") for address in addresses
    ):
        raise RuntimeError(
            "temporary redirect verification failed: %s resolved to %s"
            % (HOSTNAME, ", ".join(addresses) or "no address")
        )
    return addresses


def _validate_runtime() -> dict[str, object]:
    """Fail closed before attaching or changing hosts when assets are absent."""
    result = audit_beta_runtime()
    if not result.get("ready"):
        errors = result.get("errors") or ["unknown runtime preflight failure"]
        raise RuntimeError("runtime preflight failed: %s" % "; ".join(errors))
    emit(
        "eaapp-full-runtime-preflight-ok",
        counts=result.get("counts", {}),
        warningCount=len(result.get("warnings", [])),
    )
    return result


def _relay_output(process: subprocess.Popen[str]) -> None:
    if process.stdout is None:
        return
    for line in process.stdout:
        print(line.rstrip("\r\n"), flush=True)


# Every block and hostname this project has ever written into hosts. The
# recovery below has to recognise all of them: knowing only the certificate
# probe's markers stranded a tester whose hosts still carried a different
# leftover, and no launch could ever clear it again.
_LOCALFUT_HOSTS_BLOCKS = (
    (b"# BEGIN LOCALFUT19_EAAPP_CERT_PROBE",
     b"# END LOCALFUT19_EAAPP_CERT_PROBE"),
    (b"# BEGIN LOCALFUT19 LEGACY SESSION",
     b"# END LOCALFUT19 LEGACY SESSION"),
)
# Matched as bare tokens rather than full hostnames. The project writes both
# "spring18.gosredirector.ea.com" and "gosredirector.ea.com", and a pattern
# pinned to the longer form left the shorter one behind - which the broader
# check then reported as still present, so a cleanup could announce success
# and refuse in the same breath. No hosts entry contains these tokens unless
# it is one of ours.
_LOCALFUT_HOSTNAMES = rb"(?:LOCALFUT19|gosredirector|gosca18)"


def strip_localfut_hosts_lines(current: bytes) -> bytes:
    """Remove every line this project writes, leaving all others untouched."""
    cleaned = current
    for start, end in _LOCALFUT_HOSTS_BLOCKS:
        # Anchored at the start of the block's own line. Consuming the newline
        # *before* the block would join the two surrounding lines together and
        # corrupt an entry that belongs to the machine.
        cleaned = _re.sub(
            rb"(?im)^[^\r\n]*" + _re.escape(start) + rb".*?" +
            _re.escape(end) + rb"[^\r\n]*(?:\r?\n)?",
            b"", cleaned, flags=_re.DOTALL)
    # A block whose end marker was lost leaves its redirect behind, and a
    # cleanup that removed only the redirect leaves the comments behind.
    cleaned = _re.sub(
        rb"(?im)^[^\r\n]*" + _LOCALFUT_HOSTNAMES +
        rb"[^\r\n]*(?:\r?\n)?", b"", cleaned)
    return cleaned


# The stock Windows hosts file. Used only when stripping our own lines would
# leave nothing at all, which means the file already held nothing else.
DEFAULT_WINDOWS_HOSTS = b"\r\n".join([
    b"# Copyright (c) 1993-2009 Microsoft Corp.",
    b"#",
    b"# This is a sample HOSTS file used by Microsoft TCP/IP for Windows.",
    b"#",
    b"# This file contains the mappings of IP addresses to host names. Each",
    b"# entry should be kept on an individual line. The IP address should",
    b"# be placed in the first column followed by the corresponding host name.",
    b"# The IP address and the host name should be separated by at least one",
    b"# space.",
    b"#",
    b"# Additionally, comments (such as these) may be inserted on individual",
    b"# lines or following the machine name denoted by a '#' symbol.",
    b"#",
    b"# For example:",
    b"#",
    b"#      102.54.94.97     rhino.acme.com          # source server",
    b"#       38.25.63.10     x.acme.com              # x client host",
    b"",
    b"# localhost name resolution is handled within DNS itself.",
    b"#\t127.0.0.1       localhost",
    b"#\t::1             localhost",
    b"",
])


def safe_restore_hosts(path: Path, original: bytes) -> str:
    """Put hosts back, and never leave our redirect behind.

    A snapshot taken from an already-damaged hosts file can be empty, and
    writing an empty hosts file is refused - correctly, because that is how
    the file was lost in the first place. But refusing there also aborted the
    shutdown and left our redirect in place, which is the worse outcome:
    every EA connection stays pointed at this machine after the session ends.

    Removing our own lines from whatever is on disk is always correct and
    always possible, so that is the fallback.
    """
    if original and original.strip():
        restore_hosts(path, original)
        return "snapshot"
    cleaned = strip_localfut_hosts_lines(path.read_bytes())
    if not cleaned.strip():
        # Stripping emptied the file, so nothing but our own lines was left
        # in it. Write the stock Windows content rather than an empty file.
        cleaned = DEFAULT_WINDOWS_HOSTS
    restore_hosts(path, cleaned)
    return "cleaned"


def _recover_stale_hosts_snapshot(path: Path, snapshot_path: Path) -> None:
    """Undo a redirect left behind by an interrupted session.

    This must never refuse. The snapshot exists precisely because a session
    was killed instead of stopped, and a refusal here is unrecoverable for the
    user: nothing in the normal flow clears the state, so every later launch
    fails the same way. Removing our own lines while keeping every other line
    also preserves an edit made to hosts between sessions.
    """
    if not snapshot_path.exists():
        return
    saved = snapshot_path.read_bytes()
    current = path.read_bytes()
    if current == saved:
        snapshot_path.unlink()
        emit("hosts-stale-snapshot-cleared", status="already-restored")
        return
    cleaned = strip_localfut_hosts_lines(current)
    if cleaned != current:
        # Prefer the byte-exact snapshot when our own lines are the only
        # difference; otherwise keep the machine's own later edits.
        byte_exact = cleaned == saved == strip_localfut_hosts_lines(saved)
        restore_hosts(path, saved if byte_exact else cleaned)
        snapshot_path.unlink()
        emit("hosts-recovered-from-stale-snapshot",
             status="byte-exact" if byte_exact else "local-lines-removed")
        return
    # Nothing of ours is left in hosts, so there is nothing to undo and the
    # differences belong to the machine.
    snapshot_path.unlink()
    emit("hosts-stale-snapshot-discarded", status="no-local-redirect")


def _load_guard(
    session: object,
    scripts: list[object],
    name: str,
    source: str,
    request_handler=None,
    request_event: str = "fut-champions-offline-entry-request",
    reply_type: str = "fut-champions-offline-entry-result",
) -> dict[str, object]:
    armed = threading.Event()
    state: dict[str, object] = {}
    latest: dict[str, object] = {}
    request_lock = threading.Lock()
    script = None

    def answer_request(payload: dict[str, object]) -> None:
        request_id = int(payload.get("requestId", 0) or 0)
        if not request_lock.acquire(blocking=False):
            result = {
                "requestId": request_id,
                "status": "error",
                "reason": "entry-request-already-active",
            }
        else:
            try:
                result = dict(request_handler(payload) or {})
            except Exception as error:
                result = {
                    "status": "error",
                    "reason": type(error).__name__,
                }
            finally:
                request_lock.release()
            result["requestId"] = request_id
        try:
            if script is not None:
                script.post({
                    "type": reply_type,
                    "payload": result,
                })
            emit("eaapp-full-guard-reply", guard=name, payload=result)
        except Exception as error:
            emit("eaapp-full-guard-reply-failed", guard=name,
                 requestId=request_id, reason=str(error))

    def on_message(message, _data):
        if message.get("type") == "send":
            payload = message.get("payload") or {}
        else:
            payload = {
                "event": "agent-error",
                "description": message.get("description"),
            }
        latest.update(payload)
        emit("eaapp-full-guard-message", guard=name, payload=payload)
        event = payload.get("event")
        if (request_handler is not None and
                event == request_event):
            # The intercepted FIFA UI thread waits for this exact reply.  Run
            # the topmost modal on its own host thread so Frida can continue
            # delivering messages and the reply can always release the hook.
            threading.Thread(
                target=answer_request,
                args=(dict(payload),),
                daemon=True,
            ).start()
        if event in ("armed", "refused", "agent-error"):
            state.update(payload)
            armed.set()

    script = session.create_script(source)
    script.on("message", on_message)
    script.load()
    scripts.append(script)
    if not armed.wait(10.0):
        raise RuntimeError("%s guard did not arm" % name)
    if state.get("event") != "armed":
        raise RuntimeError(
            "%s guard refused: %s"
            % (name, state.get("reason", state.get("event")))
        )
    emit("eaapp-full-guard-armed", guard=name, pid=state.get("pid"))
    return latest


def _load_observer(
    session: object,
    scripts: list[object],
    name: str,
    source: str,
) -> dict[str, object]:
    """Load a read-only observer without making the session depend on it.

    An observer only records evidence, so a refusal or a load failure must
    never stop a run that would otherwise work. Guards keep using
    ``_load_guard`` and still fail closed.
    """
    armed = threading.Event()
    state: dict[str, object] = {}

    def on_message(message, _data):
        if message.get("type") == "send":
            payload = message.get("payload") or {}
        else:
            payload = {
                "event": "agent-error",
                "description": message.get("description"),
            }
        emit("eaapp-full-observer-message", observer=name, payload=payload)
        event = payload.get("event")
        if event in ("armed", "refused", "agent-error"):
            state.update(payload)
            armed.set()

    try:
        script = session.create_script(source)
        script.on("message", on_message)
        script.load()
        scripts.append(script)
    except Exception as error:  # observation must never break the run
        emit("eaapp-full-observer-unavailable", observer=name,
             reason=str(error))
        return {}
    if not armed.wait(10.0):
        emit("eaapp-full-observer-unavailable", observer=name,
             reason="observer did not arm")
        return {}
    if state.get("event") != "armed":
        emit("eaapp-full-observer-unavailable", observer=name,
             reason=str(state.get("reason", state.get("event"))))
        return {}
    emit("eaapp-full-observer-armed", observer=name, state=state.get("state"))
    return state


def main() -> int:
    args = parse_args()
    if not ctypes.windll.shell32.IsUserAnAdmin():
        raise RuntimeError("run this diagnostic as Administrator")
    _validate_runtime()
    executable = verify_target(args.pid)
    emit("preflight-ok", pid=args.pid, imageName=os.path.basename(executable))

    data_root = Path(args.data_root).resolve()
    data_root.mkdir(parents=True, exist_ok=True)
    network_log = Path(args.network_log).resolve()
    try:
        network_log.relative_to(data_root)
    except ValueError as error:
        raise RuntimeError("network log must stay inside the data root") from error
    network_log.parent.mkdir(parents=True, exist_ok=True)
    stop_file = Path(args.stop_file).resolve()
    try:
        stop_file.relative_to(data_root)
    except ValueError as error:
        raise RuntimeError("stop file must stay inside the data root") from error
    stop_file.parent.mkdir(parents=True, exist_ok=True)
    if stop_file.exists():
        stop_file.unlink()

    path = hosts_path()
    hosts_snapshot = data_root / "eaapp-full-hosts.original.bin"
    _recover_stale_hosts_snapshot(path, hosts_snapshot)
    original_hosts = path.read_bytes()
    hosts_changed = False
    server_process: subprocess.Popen[str] | None = None
    session = None
    scripts: list[object] = []
    detached = threading.Event()
    try:
        environment = os.environ.copy()
        environment["LOCALFUT19_DATA_ROOT"] = str(data_root)
        environment["LOCALFUT19_BUILD_PROFILE"] = (
            "fifa19-pc-eaapp-4052077-observed"
        )
        environment["LOCALFUT19_PROFILE"] = args.account_mode
        if args.account_mode == "RTG":
            environment["LOCALFUT19_RTG_MODE"] = "1"
        else:
            environment.pop("LOCALFUT19_RTG_MODE", None)
        environment["LOCALFUT19_NETWORK_LOG"] = str(network_log)
        helper = os.path.join(TOOLS_DIR, "serve_localfut19_network_only.py")
        # A session that was killed instead of stopped leaves its server
        # running and holding the ports. The next launch then failed with
        # "exited with code 4" and the user had to hunt a stray python.exe in
        # Task Manager. End our own leftover first; anything else is left
        # alone and still reported.
        stale = _clear_stale_localfut_servers()
        if stale:
            print("Closed %d leftover LocalFUT19 server process%s from a "
                  "previous session." % (len(stale),
                                         "" if len(stale) == 1 else "es"),
                  flush=True)
        server_process = subprocess.Popen(
            [sys.executable, "-B", helper],
            cwd=ROOT,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        threading.Thread(
            target=_relay_output,
            args=(server_process,),
            daemon=True,
        ).start()
        _wait_for_ports(server_process)
        emit("eaapp-full-network-ready", ports=list(SERVICE_PORTS))

        import frida

        session = _attach_with_retry(frida.get_local_device(), args.pid)

        def on_detached(reason, *_details):
            emit("eaapp-full-process-detached", pid=args.pid, reason=str(reason))
            detached.set()

        session.on("detached", on_detached)
        certificate_state = _load_guard(
            session,
            scripts,
            "certificate",
            certificate_guard_agent_source(),
        )
        if args.manual_continue:
            _load_guard(
                session,
                scripts,
                "redirector-endpoint",
                redirector_endpoint_guard_agent_source(),
            )
        _load_guard(
            session,
            scripts,
            "eligibility",
            eligibility_guard_agent_source(),
        )
        _load_guard(
            session,
            scripts,
            "origin-session",
            origin_session_guard_agent_source(),
        )
        # The Draft AWAY accessor guard is not loaded: it never fired for a
        # Draft match (RC122/RC123) and was the only extra native event at the
        # RC156/RC157 TOTW freeze boundary. It still ships for evidence runs.
        # The opponent-clubs redirect is not loaded either: RC159 live applied
        # the server's club through OPPONENT_CLUBS and the Draft still showed
        # Manchester City, so FIFA19.exe does not read it there.
        # These two exact SBC reward identities already carry five in the
        # server payload, but Player Bio publishes the native zero instead.
        _load_guard(
            session,
            scripts,
            "player-skill-moves",
            player_skill_moves_guard_agent_source(),
        )
        if EXPERIMENTAL_CHAMPIONS_ENABLED:
            # The unfinished native entry and CPU-match bridges remain
            # available for later evidence runs, but the first beta must not
            # arm them while the server advertises Champions as unavailable.
            _load_guard(
                session,
                scripts,
                "fut-champions-registration",
                fut_champions_registration_guard_agent_source(),
                request_handler=handle_offline_entry_request,
            )
            _load_guard(
                session,
                scripts,
                "fut-champions-cpu-match",
                fut_champions_cpu_match_guard_agent_source(),
                request_handler=handle_offline_match_request,
                request_event="fut-champions-cpu-match-request",
                reply_type="fut-champions-cpu-match-result",
            )
        # Read-only pack-opening evidence. CardsDLL is loaded when FUT starts,
        # so this attaches later in the session and reports where the native
        # create-pack path stops. It changes no state and no control flow.
        # Native exceptions are also written straight to this file, because a
        # hard process termination kills the agent before an asynchronous
        # message can reach the host.
        crash_report = network_log.with_name(
            network_log.name.replace(".network.log", "") + ".native-crash.log")
        _load_observer(
            session,
            scripts,
            "pack-opening",
            pack_opening_observer_agent_source(str(crash_report)),
        )
        # The live failure happens before any SBC save or item assignment.
        # Record the existing UI return context so the next correction is
        # based on the client's own INDEX/UUID lookup rather than a new field.
        _load_observer(
            session,
            scripts,
            "sbc-market-return",
            sbc_market_return_observer_agent_source(),
        )
        # The AWAY club every offline mode loads is decided by one cache
        # search. TOTW (mode 1008) is marked; the Draft is mode 1002 and
        # fails closed. Record what that search sees.
        _load_observer(
            session,
            scripts,
            "offline-select-away",
            offline_select_away_observer_agent_source(),
        )
        _load_observer(
            session,
            scripts,
            "season-event-cards",
            season_event_card_observer_agent_source(),
        )
        _load_observer(
            session,
            scripts,
            "season-selection",
            season_selection_observer_agent_source(),
        )
        # Which divisions FIFA asks the catalogue for, and which function
        # filled them. Entry asks for client division 1 and every post-match
        # return asks for 0, which cannot exist; three different answers were
        # disproved live, so the source is read here instead of guessed.
        _load_observer(
            session,
            scripts,
            "season-list-request",
            season_list_request_observer_agent_source(),
        )
        if EXPERIMENTAL_CHAMPIONS_ENABLED:
            _load_observer(
                session,
                scripts,
                "fut-champions-actions",
                fut_champions_action_observer_agent_source(),
            )
            _load_observer(
                session,
                scripts,
                "fut-champions-match-transition",
                fut_champions_match_transition_observer_agent_source(),
            )
        emit("eaapp-full-native-crash-report", path=str(crash_report))
        if certificate_state.get("event") == "unexpected-return":
            raise RuntimeError(
                "FIFA completed a remote certificate check before temporary "
                "routing was installed; close FIFA and retry"
            )
        remote = _remote_redirector_connections(args.pid)
        if remote:
            raise RuntimeError(
                "FIFA already owns a remote redirector connection: %s"
                % ", ".join(remote)
            )

        with hosts_snapshot.open("xb") as stream:
            stream.write(original_hosts)
            stream.flush()
            os.fsync(stream.fileno())
        if hosts_snapshot.read_bytes() != original_hosts:
            raise RuntimeError("hosts recovery snapshot verification failed")
        emit("hosts-snapshot-ready", status="byte-exact")
        install_temporary_hosts(path, original_hosts)
        hosts_changed = True
        emit("hosts-installed", scope="eaapp-full-network-isolated-profile")
        resolved_addresses = _verify_local_redirector_resolution()
        emit(
            "eaapp-full-routing-verified",
            hostname=HOSTNAME,
            addresses=resolved_addresses,
        )
        remote = _remote_redirector_connections(args.pid)
        if remote:
            raise RuntimeError(
                "FIFA retained a remote redirector connection: %s"
                % ", ".join(remote)
            )
        if certificate_state.get("event") == "unexpected-return":
            raise RuntimeError(
                "FIFA used the remote redirector before local routing became active"
            )
        emit(
            "ready-enter-fut-full-server",
            databaseScope=(
                "eaapp-rtg-isolated" if args.account_mode == "RTG"
                else "eaapp-normal-isolated"
            ),
            accountMode=args.account_mode,
            stop="tools/stop_eaapp_full_server.ps1 or close FIFA",
        )
        print(
            "\nLOCALFUT19 SERVER READY - You can enter FUT now.\n",
            flush=True,
        )
        _force_console_redraw(
            "LOCALFUT19 SERVER READY - You can enter FUT now"
        )
        last_route_check = 0.0
        while not detached.wait(0.5):
            if stop_file.exists():
                emit("eaapp-full-stop-requested", source="stop-file")
                close_game(args.pid, detached)
                break
            if server_process.poll() is not None:
                raise RuntimeError(
                    _describe_server_exit(server_process.returncode)
                )
            if certificate_state.get("event") == "unexpected-return":
                raise RuntimeError(
                    "FIFA bypassed the local redirector; close FIFA and retry"
                )
            now = time.monotonic()
            if now - last_route_check >= 2.0:
                remote = _remote_redirector_connections(args.pid)
                if remote:
                    raise RuntimeError(
                        "FIFA opened a remote redirector connection: %s"
                        % ", ".join(remote)
                    )
                last_route_check = now
    except KeyboardInterrupt:
        emit("eaapp-full-stop-requested", source="keyboard")
    finally:
        restore_error = None
        try:
            if hosts_changed or path.read_bytes() != original_hosts:
                emit("hosts-restored-on-exit",
                     status=safe_restore_hosts(path, original_hosts))
            if path.read_bytes() == original_hosts and hosts_snapshot.exists():
                hosts_snapshot.unlink()
        except BaseException as error:
            restore_error = error
        if server_process is not None and server_process.poll() is None:
            server_process.terminate()
            try:
                server_process.wait(timeout=10.0)
            except subprocess.TimeoutExpired:
                server_process.kill()
                server_process.wait(timeout=5.0)
        if session is not None:
            for script in reversed(scripts):
                try:
                    script.unload()
                except Exception:
                    pass
            try:
                session.detach()
            except Exception:
                pass
        if stop_file.exists():
            stop_file.unlink()
        if restore_error is not None:
            # Failing here used to end the session with a traceback while our
            # redirect stayed in hosts, which points every EA connection at
            # this machine until someone notices. Clearing our own lines is a
            # last resort that always works; only a hosts file still carrying
            # them is worth failing over.
            emit("hosts-restore-failed", error=str(restore_error))
            try:
                current = path.read_bytes()
                cleared = strip_localfut_hosts_lines(current)
                if cleared != current:
                    restore_hosts(path, cleared or DEFAULT_WINDOWS_HOSTS)
                    emit("hosts-cleared-after-failed-restore")
                    restore_error = None
            except BaseException as error:
                emit("hosts-clear-failed", error=str(error))
        if restore_error is not None:
            raise RuntimeError(
                _describe_stranded_hosts(path, restore_error)
            ) from restore_error
    emit("eaapp-full-complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
