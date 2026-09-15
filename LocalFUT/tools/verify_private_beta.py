#!/usr/bin/env python3
"""Verify a staged LocalFUT19 EA App private beta.

The build-time check is intentionally stricter than the tester check: a clean
stage must not contain game binaries or generated local state.  After setup,
the tester check verifies the immutable package manifest and separately
fingerprints the user's installed FIFA 19 EXE + CardsDLL pair.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

# Verification must be read-only even when it dynamically imports the staged
# detector and runtime preflight from an extracted package.
sys.dont_write_bytecode = True


HERE = Path(__file__).resolve()
LOCALFUT_ROOT = HERE.parents[1]
DEFAULT_PACKAGE_ROOT = (
    LOCALFUT_ROOT.parent if LOCALFUT_ROOT.name.lower() == "localfut"
    else LOCALFUT_ROOT
)

REQUIRED_PACKAGE_FILES = (
    "0_READ_INSTRUCTIONS_FIRST.md",
    "FUT_DEBA_LAUNCHER.cmd",
    "ADVANCED/README.txt",
    "ADVANCED/PLAY_WITHOUT_LAUNCHER.cmd",
    "ADVANCED/CLEANUP_OLD_REDIRECTS.cmd",
    "ADVANCED/RTG_ACCOUNT_MODE/README.txt",
    "ADVANCED/RTG_ACCOUNT_MODE/PLAY_RTG_ACCOUNT.cmd",
    "ADVANCED/RTG_ACCOUNT_MODE/RESET_RTG_ACCOUNT.cmd",
    "ADVANCED/TEST_CHECKLIST.md",
    "ADVANCED/BUG_REPORT_TEMPLATE.md",
    "LEGAL_NOTICE.txt",
    "BUILD_INFO.json",
    "PACKAGE_SHA256.txt",
    "LocalFUT/requirements-beta.txt",
    "LocalFUT/SETUP_RUNTIME.cmd",
    "LocalFUT/ADD_COINS.cmd",
    "LocalFUT/ADD_DRAFT_TOKENS.cmd",
    "LocalFUT/MIGRATE_V1_ACCOUNT.cmd",
    "LocalFUT/RESET_ACCOUNT.cmd",
    "LocalFUT/RESET_CLUB.cmd",
    "LocalFUT/RESET_DRAFT.cmd",
    "LocalFUT/RESET_SBC.cmd",
    "LocalFUT/SET_DRAFT_QUALITY.cmd",
    "LocalFUT/FUT_DEBA_LAUNCHER.cmd",
    "LocalFUT/server/local_fut19_server.py",
    "LocalFUT/server/fut_compat.py",
    "LocalFUT/server/fut_seasons.py",
    "LocalFUT/PLAY_FUT19_LOCAL.cmd",
    "LocalFUT/tools/beta_readiness.py",
    "LocalFUT/tools/champions_offline_entry.py",
    "LocalFUT/tools/detect_fifa19_build.py",
    "LocalFUT/tools/futdeba_launcher.py",
    "LocalFUT/tools/futdeba_launcher_action.ps1",
    "LocalFUT/tools/guard_eaapp_origin_eligibility_missing_text.py",
    "LocalFUT/tools/guard_eaapp_origin_session_missing_values.py",
    "LocalFUT/tools/guard_eaapp_fut_champions_registration.py",
    "LocalFUT/tools/guard_eaapp_fut_champions_cpu_match.py",
    "LocalFUT/tools/guard_eaapp_draft_away_club.py",
    "LocalFUT/tools/guard_eaapp_draft_opponent_clubs.py",
    "LocalFUT/tools/observe_eaapp_season_list_request.py",
    "LocalFUT/tools/guard_eaapp_totw_away_participant.py",
    "LocalFUT/tools/guard_eaapp_player_skill_moves.py",
    "LocalFUT/tools/guard_eaapp_season_current_id.py",
    "LocalFUT/tools/migrate_v1_account.py",
    "LocalFUT/tools/observe_eaapp_certificate_return.py",
    "LocalFUT/tools/observe_eaapp_origin_session_boundary.py",
    "LocalFUT/tools/observe_eaapp_pack_opening_stall.py",
    "LocalFUT/tools/observe_eaapp_fut_champions_actions.py",
    "LocalFUT/tools/observe_eaapp_fut_champions_match_transition.py",
    "LocalFUT/tools/observe_eaapp_fut_communication_error.py",
    "LocalFUT/tools/observe_eaapp_player_skill_moves.py",
    "LocalFUT/tools/observe_eaapp_offline_select_away.py",
    "LocalFUT/tools/observe_eaapp_season_event_cards.py",
    "LocalFUT/tools/observe_eaapp_season_selection.py",
    "LocalFUT/tools/live_draft_opponent_identity_probe.py",
    "LocalFUT/tools/observe_eaapp_sbc_market_return.py",
    "LocalFUT/tools/probe_eaapp_certificate_signature.py",
    "LocalFUT/tools/probe_eaapp_local_certificate_at_menu.py",
    "LocalFUT/tools/probe_eaapp_local_certificate_return.py",
    "LocalFUT/tools/probe_eaapp_local_certificate_success_at_menu.py",
    "LocalFUT/tools/refresh_fut_cache.ps1",
    "LocalFUT/tools/reset_rtg_account.ps1",
    "LocalFUT/tools/remove_localfut_hosts.ps1",
    "LocalFUT/tools/run_eaapp_full_server_guarded_at_menu.py",
    "LocalFUT/tools/run_eaapp_full_server_guarded_at_menu.ps1",
    "LocalFUT/tools/run_legacy_full_server.py",
    "LocalFUT/tools/serve_localfut19_network_only.py",
    "LocalFUT/tools/session_preflight.py",
    "LocalFUT/tools/start_localfut19.ps1",
    "LocalFUT/tools/verify_private_beta.py",
    "LocalFUT/tools/launcher_assets/README.txt",
    "LocalFUT/tools/launcher_assets/futdeba-hero-v2.png",
    "LocalFUT/tools/launcher_assets/futdeba-brand-v2.png",
    "LocalFUT/tools/launcher_assets/launcher-icon-v3.ico",
    "LocalFUT/tools/launcher_assets/launcher-icon-v3.png",
)

DISTRIBUTED_CMD_FILES = tuple(
    relative for relative in REQUIRED_PACKAGE_FILES
    if relative.lower().endswith(".cmd")
)

FORBIDDEN_STAGE_NAMES = {
    "draft_quality.json",
    "hosts.backup",
    "messaggio_codex.md",
    "server.log",
}
FORBIDDEN_STAGE_SUFFIXES = {
    ".7z", ".cas", ".cat", ".dll", ".exe", ".key", ".log", ".pem",
    ".rar", ".sb", ".sqlite", ".sqlite3", ".toc", ".zip",
}
FORBIDDEN_STAGE_PARTS = {
    ".git", ".runtime", "backups", "certs", "diagnostics", "research",
    "source_png", "__pycache__",
}
RELEASE_TEXT_SUFFIXES = {
    ".cmd", ".csv", ".json", ".md", ".ps1", ".py", ".txt",
}
FORBIDDEN_RELEASE_TEXT_MARKERS = (
    "fut" + "bin",
    "fut" + "wiz",
    "ea " + "catalogue",
    "ea " + "catalog",
    "ea " + "cdn",
    "ea_" + "cdn",
    "ea-" + "cdn",
    "cdn." + "ea",
    "ea" + "assets",
    "downloaded " + "from",
    "extracted " + "from",
)
ITALIAN_RELEASE_WORDS = (
    "questa", "cartella", "riconosciute", "esegui", "avvio", "chiusura",
    "gioco", "nessun", "nessuna", "crediti", "pacchetti", "partite",
    "mercato", "squadre", "segnalazione", "limiti", "rimuovere",
    "premi", "rosa", "oggetti", "rimane", "restano", "viene",
)
PERSONAL_COMMENT_MARKERS = (
    "co" + "dex", "clau" + "de", "my man", "btw",
)
WINDOWS_USER_PATH = re.compile(
    r"(?i)[a-z]:[/\\]users[/\\][^/\\\s\"']+"
)
CMD_LOCAL_REFERENCE = re.compile(
    r'(?i)"%~dp0([^"\r\n]+\.(?:cmd|ps1|py|txt))"'
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalized_relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def verify_manifest(package_root: Path) -> list[str]:
    errors: list[str] = []
    manifest_path = package_root / "PACKAGE_SHA256.txt"
    if not manifest_path.is_file():
        return ["missing PACKAGE_SHA256.txt"]
    try:
        lines = manifest_path.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeError) as exc:
        return [f"cannot read PACKAGE_SHA256.txt: {exc}"]

    seen: set[str] = set()
    for line_number, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if " *" not in line:
            errors.append(f"invalid manifest line {line_number}")
            continue
        expected, relative = line.split(" *", 1)
        relative = relative.replace("\\", "/").lstrip("/")
        if (len(expected) != 64 or
                any(char not in "0123456789abcdefABCDEF" for char in expected)):
            errors.append(f"invalid SHA-256 on manifest line {line_number}")
            continue
        if not relative or relative in seen:
            errors.append(f"invalid or duplicate path on manifest line {line_number}")
            continue
        seen.add(relative)
        target = (package_root / Path(relative)).resolve()
        try:
            target.relative_to(package_root.resolve())
        except ValueError:
            errors.append(f"manifest path escapes the package: {relative}")
            continue
        if not target.is_file():
            errors.append(f"missing immutable package file: {relative}")
            continue
        actual = _sha256(target)
        if actual.lower() != expected.lower():
            errors.append(f"hash mismatch: {relative}")
    if not seen:
        errors.append("PACKAGE_SHA256.txt contains no file hashes")
    return errors


def _load_staged_compat(localfut_root: Path):
    module_path = localfut_root / "server" / "fut_compat.py"
    if not module_path.is_file():
        raise FileNotFoundError("missing LocalFUT/server/fut_compat.py")
    module_name = "_localfut19_staged_fut_compat"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise ImportError("cannot load staged fut_compat.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(module_name, None)
    return module


def verify_build_metadata(package_root: Path) -> list[str]:
    """Keep package claims synchronized with the staged compatibility registry."""
    errors: list[str] = []
    info_path = package_root / "BUILD_INFO.json"
    if not info_path.is_file():
        return ["missing BUILD_INFO.json"]
    try:
        info = json.loads(info_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return [f"cannot read BUILD_INFO.json: {exc}"]
    if not isinstance(info, dict):
        return ["BUILD_INFO.json root must be an object"]
    if info.get("schemaVersion") != 2:
        errors.append("BUILD_INFO.json schemaVersion must be 2")

    try:
        compat = _load_staged_compat(package_root / "LocalFUT")
        profiles = tuple(compat.KNOWN_BUILD_PROFILES)
    except (AttributeError, ImportError, OSError) as exc:
        return errors + [f"cannot inspect staged build registry: {exc}"]

    expected = {
        profile.profile_id: {
            "productVersion": profile.executable.product_version,
            "status": profile.support_status,
            "selectionPriority": profile.selection_priority,
        }
        for profile in profiles
    }
    raw_rows = info.get("knownGameBuilds")
    if not isinstance(raw_rows, list):
        return errors + ["BUILD_INFO.json knownGameBuilds must be an array"]
    actual: dict[str, dict] = {}
    for row in raw_rows:
        if not isinstance(row, dict) or not isinstance(row.get("profileId"), str):
            errors.append("BUILD_INFO.json contains an invalid knownGameBuilds row")
            continue
        profile_id = row["profileId"]
        if profile_id in actual:
            errors.append(f"BUILD_INFO.json duplicates profile {profile_id}")
            continue
        actual[profile_id] = row
    for profile_id in sorted(set(expected) - set(actual)):
        errors.append(f"BUILD_INFO.json is missing profile {profile_id}")
    for profile_id in sorted(set(actual) - set(expected)):
        errors.append(f"BUILD_INFO.json contains unknown profile {profile_id}")
    for profile_id in sorted(set(expected) & set(actual)):
        for field, expected_value in expected[profile_id].items():
            if actual[profile_id].get(field) != expected_value:
                errors.append(
                    f"BUILD_INFO.json {profile_id} {field} does not match "
                    "the staged compatibility registry")

    policy = info.get("selectionPolicy")
    if not isinstance(policy, dict):
        return errors + ["BUILD_INFO.json selectionPolicy must be an object"]
    if policy.get("mode") != "unique-highest-known-profile-priority":
        errors.append("BUILD_INFO.json selectionPolicy mode is invalid")
    if policy.get("failClosedOnTie") is not True:
        errors.append("BUILD_INFO.json selectionPolicy must fail closed on ties")
    overrides = policy.get("explicitOverrides")
    if (not isinstance(overrides, list) or
            any(not isinstance(value, str) for value in overrides) or
            set(overrides) != {"-GamePath", "-ProfileId"}):
        errors.append("BUILD_INFO.json explicit selection overrides are invalid")
    if profiles:
        highest = max(profile.selection_priority for profile in profiles)
        preferred = [profile.profile_id for profile in profiles
                     if profile.selection_priority == highest]
        expected_default = preferred[0] if len(preferred) == 1 else None
        if policy.get("defaultProfileId") != expected_default:
            errors.append(
                "BUILD_INFO.json defaultProfileId does not match the unique "
                "highest-priority staged profile")
    return errors


def verify_release_text(package_root: Path) -> list[str]:
    """Reject source-acquisition details, Italian docs, and personal comments."""
    errors: list[str] = []
    package_root = package_root.resolve()
    for path in sorted(package_root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in RELEASE_TEXT_SUFFIXES:
            continue
        relative = _normalized_relative(path, package_root)
        lower_relative = relative.lower()
        for marker in FORBIDDEN_RELEASE_TEXT_MARKERS:
            if marker in lower_relative:
                errors.append(
                    f"private acquisition marker in staged path: {relative}")
                break
        try:
            content = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError) as exc:
            errors.append(f"cannot inspect staged text file {relative}: {exc}")
            continue
        lowered = content.lower()
        for marker in FORBIDDEN_RELEASE_TEXT_MARKERS:
            if marker in lowered:
                errors.append(
                    f"private acquisition marker in staged text: {relative}")
                break
        if WINDOWS_USER_PATH.search(content):
            errors.append(f"personal absolute path in staged text: {relative}")
        if path.suffix.lower() in {".md", ".txt"}:
            for word in ITALIAN_RELEASE_WORDS:
                if re.search(r"(?i)(?<!\w)%s(?!\w)" % re.escape(word), content):
                    errors.append(
                        f"non-English release prose in {relative}: {word}")
                    break
        if path.suffix.lower() == ".py":
            try:
                compile(content, relative, "exec")
            except SyntaxError as exc:
                errors.append(
                    f"invalid staged Python source {relative}: "
                    f"line {exc.lineno}: {exc.msg}")
            for line in content.splitlines():
                comment = line.split("#", 1)[1].lower() if "#" in line else ""
                if any(marker in comment for marker in PERSONAL_COMMENT_MARKERS):
                    errors.append(
                        f"personal development comment in staged source: {relative}")
                    break
    return errors


def verify_staged_server_import(package_root: Path) -> list[str]:
    """Import the sanitized server without touching the tester's profile."""
    server_dir = package_root / "LocalFUT" / "server"
    server_path = server_dir / "local_fut19_server.py"
    if not server_path.is_file():
        return []
    try:
        with tempfile.TemporaryDirectory(prefix="localfut19-stage-import-") as root:
            data_root = Path(root)
            environment = os.environ.copy()
            environment["LOCALFUT19_DATA_ROOT"] = os.fspath(data_root)
            environment["LOCALFUT19_NETWORK_LOG"] = os.fspath(
                data_root / "server.log")
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
            result = subprocess.run(
                [sys.executable, "-B", "-c", "import local_fut19_server"],
                cwd=server_dir,
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=60,
                check=False,
            )
    except (OSError, subprocess.SubprocessError) as exc:
        return [f"cannot run staged server import check: {exc}"]
    if result.returncode == 0:
        return []
    output = (result.stderr or result.stdout).strip().splitlines()
    detail = output[-1] if output else f"exit code {result.returncode}"
    return [f"staged server import failed: {detail}"]


def verify_windows_entrypoints(
        package_root: Path,
        command_files: tuple[str, ...] = DISTRIBUTED_CMD_FILES) -> list[str]:
    """Lint every distributed CMD and each literal package-relative handoff."""
    errors: list[str] = []
    package_root = package_root.resolve()
    for relative in command_files:
        path = package_root / Path(relative)
        if not path.is_file():
            continue
        try:
            source = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError) as exc:
            errors.append(f"cannot inspect Windows entry point {relative}: {exc}")
            continue
        lines = [line.strip() for line in source.splitlines() if line.strip()]
        lowered = source.lower()
        if not lines or lines[0].lower() != "@echo off":
            errors.append(f"Windows entry point must start with @echo off: {relative}")
        if "setlocal enableextensions" not in lowered:
            errors.append(f"Windows entry point does not enable extensions: {relative}")
        if 'cd /d "%~dp0"' not in lowered:
            errors.append(f"Windows entry point does not anchor its directory: {relative}")
        if "exit /b" not in lowered:
            errors.append(f"Windows entry point does not return an exit code: {relative}")
        if "start-process -filepath '%~f0'" in lowered:
            errors.append(
                f"Windows entry point has quote-sensitive UAC elevation: {relative}")
        for line_number, line in enumerate(source.splitlines(), 1):
            if (re.search(r"(?i)\bpowershell(?:\.exe)?\b", line) and
                    "-noprofile" not in line.lower()):
                errors.append(
                    f"PowerShell handoff lacks -NoProfile in {relative}:"
                    f"{line_number}")
        for match in CMD_LOCAL_REFERENCE.finditer(source):
            captured = match.group(1)
            if "%" in captured or "!" in captured:
                continue
            target = (path.parent / Path(captured.replace("\\", "/"))).resolve()
            try:
                target.relative_to(package_root)
            except ValueError:
                errors.append(
                    f"Windows entry point reference escapes package in {relative}: "
                    f"{captured}")
                continue
            if not target.is_file():
                errors.append(
                    f"Windows entry point target is missing in {relative}: {captured}")
    return errors


def verify_powershell_syntax(package_root: Path) -> list[str]:
    """Run the Windows PowerShell parser over every staged PS1 without executing it."""
    package_root = package_root.resolve()
    environment = os.environ.copy()
    environment["LOCALFUT19_PS_AUDIT_ROOT"] = os.fspath(package_root)
    parser_command = (
        "$root=[IO.Path]::GetFullPath($env:LOCALFUT19_PS_AUDIT_ROOT);"
        "$failed=$false;"
        "Get-ChildItem -LiteralPath $root -Recurse -File -Filter *.ps1 | "
        "ForEach-Object {"
        "$tokens=$null;$issues=$null;"
        "[void][Management.Automation.Language.Parser]::ParseFile("
        "$_.FullName,[ref]$tokens,[ref]$issues);"
        "foreach($issue in $issues){"
        "$failed=$true;"
        "Write-Output ($_.FullName.Substring($root.Length+1)+'|' + "
        "$issue.Extent.StartLineNumber+'|'+$issue.Message)"
        "}};"
        "if($failed){exit 1}"
    )
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-Command", parser_command],
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return [f"cannot run staged PowerShell parser check: {exc}"]
    if result.returncode == 0:
        return []
    output = (result.stdout or result.stderr).strip().splitlines()
    if not output:
        output = [f"PowerShell parser exited with code {result.returncode}"]
    return [f"invalid staged PowerShell: {line}" for line in output]


def verify_package(package_root: Path, strict_staging: bool = False) -> dict:
    errors: list[str] = []
    warnings: list[str] = []
    package_root = package_root.resolve()
    for relative in REQUIRED_PACKAGE_FILES:
        if not (package_root / Path(relative)).is_file():
            errors.append(f"missing {relative}")
    errors.extend(verify_manifest(package_root))
    if (package_root / "BUILD_INFO.json").is_file():
        errors.extend(verify_build_metadata(package_root))
    errors.extend(verify_windows_entrypoints(package_root))

    if strict_staging and package_root.is_dir():
        for path in sorted(package_root.rglob("*")):
            if path.is_symlink():
                errors.append(
                    f"symbolic links are not allowed: {_normalized_relative(path, package_root)}")
                continue
            if not path.is_file():
                continue
            relative = _normalized_relative(path, package_root)
            lower_parts = {part.lower() for part in Path(relative).parts}
            if path.name.lower() in FORBIDDEN_STAGE_NAMES:
                errors.append(f"forbidden staged file: {relative}")
            if path.suffix.lower() in FORBIDDEN_STAGE_SUFFIXES:
                errors.append(f"forbidden staged file type: {relative}")
            if lower_parts & FORBIDDEN_STAGE_PARTS:
                errors.append(f"forbidden staged directory: {relative}")

        allowed_game_files = {"README.txt"}
        if game_root.is_dir():
            unexpected = [
                _normalized_relative(path, package_root)
                for path in game_root.rglob("*")
                if path.is_file() and path.name not in allowed_game_files
            ]
            for relative in unexpected:
                errors.append(f"game file present in clean stage: {relative}")

        errors.extend(verify_release_text(package_root))
        errors.extend(verify_staged_server_import(package_root))
        errors.extend(verify_powershell_syntax(package_root))

    return {
        "ready": not errors,
        "errors": errors,
        "warnings": warnings,
    }


def verify_runtime(localfut_root: Path, check_modules: bool = True) -> dict:
    errors: list[str] = []
    warnings: list[str] = []
    tool_path = localfut_root / "tools" / "beta_readiness.py"
    if not tool_path.is_file():
        return {"ready": False, "errors": ["missing beta_readiness.py"],
                "warnings": []}
    spec = importlib.util.spec_from_file_location("beta_readiness", tool_path)
    if spec is None or spec.loader is None:
        return {"ready": False, "errors": ["cannot load beta_readiness.py"],
                "warnings": []}
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result = module.audit()
    errors.extend(result.get("errors", []))
    warnings.extend(result.get("warnings", []))
    if not check_modules:
        module_errors = [
            error for error in errors
            if not error.startswith("missing Python module ")
        ]
        errors = module_errors
    return {"ready": not errors, "errors": errors, "warnings": warnings,
            "counts": result.get("counts", {})}


def verify_game(localfut_root: Path, game_executable: Path) -> dict:
    server_dir = localfut_root / "server"
    sys.path.insert(0, os.fspath(server_dir))
    try:
        from fut_compat import fingerprint_summary, inspect_known_build
        inspection = inspect_known_build(game_executable)
    except Exception as exc:  # give the tester a concise actionable failure
        return {"ready": False, "errors": [f"game inspection failed: {exc}"],
                "warnings": []}
    if not inspection.known:
        detail = fingerprint_summary(inspection)
        reason = inspection.reason or "unsupported game build"
        if detail:
            reason += "; " + detail
        return {"ready": False, "errors": [reason], "warnings": []}
    warnings = []
    if inspection.profile.support_status == "recognized-unvalidated":
        warnings.append(
            "v1 is fingerprint-recognized but is not a validation target for "
            "this beta; its feature-parity matrix is incomplete")
    if inspection.profile.support_status == "observed-beta":
        warnings.append(
            "EA App build is recognized but remains beta: only its declared "
            "feature flags have retail evidence")
    return {
        "ready": True,
        "errors": [],
        "warnings": warnings,
        "profile": inspection.profile.profile_id,
        "supportStatus": inspection.profile.support_status,
        "launchStrategy": inspection.profile.launch_strategy,
        "fingerprints": fingerprint_summary(inspection),
    }


def detect_installed_game(localfut_root: Path) -> dict:
    detector_path = localfut_root / "tools" / "detect_fifa19_build.py"
    if not detector_path.is_file():
        return {"ready": False, "errors": ["missing build detector"],
                "warnings": []}
    spec = importlib.util.spec_from_file_location(
        "localfut19_build_detector", detector_path)
    if spec is None or spec.loader is None:
        return {"ready": False, "errors": ["cannot load build detector"],
                "warnings": []}
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result = module.detect()
    if result.get("decision") == "selected":
        return verify_game(
            localfut_root, Path(result["selected"]["executable"]))
    if result.get("decision") == "ambiguous":
        choices = ", ".join(
            str(row.get("gameDirectory"))
            for row in result.get("installations", []) if row.get("known"))
        return {"ready": False,
                "errors": ["multiple known builds found; pass --game: " + choices],
                "warnings": []}
    details = "; ".join(
        "%s: %s" % (row.get("gameDirectory"), row.get("reason"))
        for row in result.get("installations", []))
    message = str(result.get("message") or "FIFA 19 was not found")
    if details:
        message += "; " + details
    return {"ready": False, "errors": [message], "warnings": []}


def _print_section(label: str, result: dict) -> None:
    print(f"[{label}] {'READY' if result.get('ready') else 'BLOCKED'}")
    if result.get("profile"):
        print("  profile:", result["profile"])
    for key, value in result.get("counts", {}).items():
        print(f"  {key}: {value}")
    for warning in result.get("warnings", []):
        print("  WARNING:", warning)
    for error in result.get("errors", []):
        print("  ERROR:", error)


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify a LocalFUT19 beta")
    parser.add_argument("--package", type=Path, default=DEFAULT_PACKAGE_ROOT)
    parser.add_argument("--game", type=Path)
    parser.add_argument("--package-only", action="store_true")
    parser.add_argument("--strict-staging", action="store_true")
    parser.add_argument("--skip-python-modules", action="store_true")
    args = parser.parse_args()

    package_root = args.package.expanduser().resolve()
    localfut_root = package_root / "LocalFUT"
    package = verify_package(package_root, args.strict_staging)
    _print_section("package", package)
    results = [package]

    if not args.package_only:
        runtime = verify_runtime(
            localfut_root, check_modules=not args.skip_python_modules)
        _print_section("runtime", runtime)
        results.append(runtime)
        if args.game:
            game = verify_game(localfut_root, args.game.expanduser().resolve())
        else:
            game = detect_installed_game(localfut_root)
        _print_section("FIFA 19 build", game)
        results.append(game)

    ready = all(bool(result.get("ready")) for result in results)
    print("\nPRIVATE BETA CHECK:", "READY" if ready else "BLOCKED")
    return 0 if ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
