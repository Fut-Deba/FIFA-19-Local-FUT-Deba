#!/usr/bin/env python3
"""Local, read-only preflight for a LocalFUT19 private beta candidate.

The source repository intentionally excludes extracted databases and mirrored
FUT content.  A copied beta directory can therefore look complete while being
unable to start.  This tool validates the local runtime inputs and the asset
containers without copying, downloading, repairing, or modifying any file.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import struct
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "server" / "data"
CONTENT = DATA / "fut_content" / "fut"

REQUIRED_JSON = (
    "players.json",
    "players_meta.json",
    "players_db.json",
    "card_versions.json",
    "carniball_versions.json",
    "ucl_moments_versions.json",
    "fut19_card_version_corrections.json",
    "fut19_sbc_reward_cards_verified.json",
    "object_catalog.json",
    "default_grant.json",
    "totw_squads.json",
    "fut19_sbc_catalog.json",
    "fut19_sbc_players_verified.json",
    "fut19_sbc_icons_verified.json",
    "fut19_sbc_leagues_verified.json",
    "fut19_sbc_upgrades_verified.json",
    "fut19_sbc_support_verified.json",
    "fut19_sbc_requirement_identities_verified.json",
    "pack_defs.json",
    "pack_weights.json",
)

REQUIRED_FILES = (
    DATA / "fifa_ng_db.DB",
    DATA / "catalog" / "fut19_players.csv",
    CONTENT / "items" / "images" / "backgrounds" / "itemBGs" /
        "futitemraritytunables.json",
    CONTENT / "packs" / "packopening" / "packopeningconfig.json",
    CONTENT / "packs" / "packopening" / "packopeningsetting.json",
    CONTENT / "playerheads" / "g4" / "fut2dheads.big",
    CONTENT / "loc" / "PC" / "leaderboards.ENG_US.xml",
    CONTENT / "loc" / "PC" / "leaderboards.ITA_IT.xml",
    CONTENT / "packs" / "loc" / "storepackdescriptions.en_us.xml",
    CONTENT / "packs" / "loc" / "storepackdescriptions.it_it.xml",
)

REQUIRED_MODULES = ("cryptography", "frida")


def _relative(path: Path) -> str:
    return str(path.relative_to(ROOT)).replace(os.sep, "/")


def _valid_dds(path: Path, exact_canvas: tuple[int, int] | None = None) -> str:
    try:
        with path.open("rb") as stream:
            header = stream.read(128)
        if len(header) != 128 or header[:4] != b"DDS ":
            return "invalid DDS header"
        compression = header[84:88]
        if compression not in {b"DXT1", b"DXT3", b"DXT5"}:
            return "unsupported DDS compression %r" % compression
        height = struct.unpack_from("<I", header, 12)[0]
        width = struct.unpack_from("<I", header, 16)[0]
        if exact_canvas and (width, height) != exact_canvas:
            return "unexpected canvas %dx%d" % (width, height)
        block_bytes = 8 if compression == b"DXT1" else 16
        expected = (128 + ((width + 3) // 4) * ((height + 3) // 4) *
                    block_bytes + 8)
        if path.stat().st_size != expected:
            return "truncated DDS (%d != %d bytes)" % (
                path.stat().st_size, expected)
    except OSError as exc:
        return str(exc)
    return ""


def audit() -> dict:
    errors: list[str] = []
    warnings: list[str] = []

    for module_name in REQUIRED_MODULES:
        if importlib.util.find_spec(module_name) is None:
            errors.append(
                "missing Python module %s; run INSTALL_PREREQUISITES.cmd" %
                module_name)

    for name in REQUIRED_JSON:
        path = DATA / name
        if not path.is_file():
            errors.append("missing %s" % _relative(path))
            continue
        try:
            with path.open("r", encoding="utf-8-sig") as stream:
                json.load(stream)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            errors.append("invalid %s: %s" % (_relative(path), exc))

    for path in REQUIRED_FILES:
        if not path.is_file() or path.stat().st_size <= 0:
            errors.append("missing or empty %s" % _relative(path))

    for path in (
            CONTENT / "loc" / "PC" / "leaderboards.ENG_US.xml",
            CONTENT / "loc" / "PC" / "leaderboards.ITA_IT.xml",
            CONTENT / "packs" / "loc" / "storepackdescriptions.en_us.xml",
            CONTENT / "packs" / "loc" / "storepackdescriptions.it_it.xml"):
        if path.is_file():
            try:
                ET.parse(path)
            except (OSError, ET.ParseError) as exc:
                errors.append("invalid %s: %s" % (_relative(path), exc))

    playerhead_dir = CONTENT / "playerheads" / "g4" / "single"
    playerheads = sorted(playerhead_dir.glob("p*.dds"))
    if len(playerheads) < 631:
        errors.append("only %d playerheads installed; expected at least 631" %
                      len(playerheads))
    for path in playerheads:
        issue = _valid_dds(path, (220, 256))
        if issue:
            errors.append("%s: %s" % (_relative(path), issue))

    background_dir = (CONTENT / "items" / "images" / "backgrounds" /
                      "itemBGs" / "large")
    backgrounds = sorted(background_dir.glob("cards_bg_e_1_*_0.dds"))
    required_rarities = {5, 30, 50, 66, 69, 71, 72, 84, 85}
    installed_rarities = set()
    for path in backgrounds:
        try:
            installed_rarities.add(int(path.stem.split("_")[-2]))
        except (IndexError, ValueError):
            warnings.append("unrecognized card-background name %s" %
                            _relative(path))
        issue = _valid_dds(path)
        if issue:
            errors.append("%s: %s" % (_relative(path), issue))
    missing_rarities = sorted(required_rarities - installed_rarities)
    if missing_rarities:
        errors.append("missing card backgrounds for rarity IDs %s" %
                      ", ".join(map(str, missing_rarities)))

    sbc_tile_dir = CONTENT / "sbc" / "gen4" / "tile"
    sbc_tiles = sorted(sbc_tile_dir.glob("*.png"))
    if not (sbc_tile_dir / "GameHub_SBS.png").is_file():
        errors.append("missing Squad Battles/SBC hub artwork")
    if len(sbc_tiles) < 300:
        errors.append("only %d SBC tile assets installed; expected at least 300" %
                      len(sbc_tiles))
    for path in sbc_tiles:
        try:
            with path.open("rb") as stream:
                header = stream.read(24)
            dimensions=(struct.unpack(">II",header[16:24])
                        if len(header)==24 else (0,0))
            if (len(header) != 24 or header[:8] != b"\x89PNG\r\n\x1a\n" or
                    header[12:16] != b"IHDR" or not all(dimensions)):
                errors.append("invalid PNG %s" % _relative(path))
        except OSError as exc:
            errors.append("invalid %s: %s" % (_relative(path), exc))

    return {
        "ready": not errors,
        "errors": errors,
        "warnings": warnings,
        "counts": {
            "pythonModules": len(REQUIRED_MODULES),
            "playerheads": len(playerheads),
            "cardBackgrounds": len(backgrounds),
            "sbcTiles": len(sbc_tiles),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate local runtime inputs before staging a beta.")
    parser.add_argument("--json", action="store_true",
                        help="print the result as JSON")
    args = parser.parse_args()
    result = audit()
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        status = "READY" if result["ready"] else "BLOCKED"
        print("LocalFUT19 beta runtime preflight: %s" % status)
        for key, value in result["counts"].items():
            print("  %-16s %d" % (key + ":", value))
        for warning in result["warnings"]:
            print("  WARNING: %s" % warning)
        for error in result["errors"]:
            print("  ERROR: %s" % error)
    return 0 if result["ready"] else 1


if __name__ == "__main__":
    sys.exit(main())
