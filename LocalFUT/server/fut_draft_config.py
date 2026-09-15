#!/usr/bin/env python3
"""User-selectable Draft quality presets for the local offline Draft."""

from __future__ import annotations

import json
import os
import sys
from typing import Any


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SETTINGS_PATH = os.path.join(PROJECT_ROOT, "draft_quality.json")
DEFAULT_PRESET = "classic"

# The guarantees are targets per five-card choice row when the role-correct
# source catalogue contains enough distinct footballers. Every preset keeps all
# sourced card revisions eligible; the higher presets only change selection
# priority and the minimum presentation quality.
PRESETS: dict[str, dict[str, Any]] = {
    "classic": {
        "label": "Classic",
        "description": "Balanced Local FUT Draft distribution.",
        "minRating": 75,
        "highRating": 88,
        "highChoices": 0,
        "specialChoices": 0,
        "featuredChoices": 0,
        "captainMinRating": 86,
        "allowWingbackFormations": True,
        # Share of each choice row taken by gold, special and Icon cards.
        # Measured on 2026-09-03, the original rating weighting produced 36.8%
        # Icons and only 10.4% gold, inverting a catalogue that is 65% gold.
        # Icons halved on 2026-09-04: at 10% a five-card row offered one often
        # enough that a full Draft handed the player five or six of them.
        "classShares": {"normal": 0.58, "special": 0.37, "icon": 0.05},
        "ratingBias": 6.0,
        # A separate pull inside each class. One shared value skewed every
        # class to the top of its own range: the specials on offer averaged
        # 92.6 and were almost all TOTS, while the ordinary Team of the Week
        # cards that make up most of a real Draft barely appeared. Flattening
        # the special class halved the 93+ cards (24.6% -> 12.1%) and brought
        # its own average from 92.6 down to 88.9 across a 75-99 spread.
        "classRatingBias": {"normal": 9.0, "special": 1.5, "icon": 3.0},
    },
    "boosted": {
        "label": "Boosted",
        "description": "More specials, with about two high-rated choices per row.",
        "minRating": 80,
        "highRating": 86,
        "highChoices": 2,
        "specialChoices": 3,
        "featuredChoices": 0,
        "captainMinRating": 87,
        "allowWingbackFormations": True,
    },
    "creator": {
        "label": "Creator",
        "description": "All promo types; about three of five choices are 88+.",
        "minRating": 82,
        "highRating": 88,
        "highChoices": 3,
        "specialChoices": 4,
        "featuredChoices": 1,
        "captainMinRating": 88,
        "allowWingbackFormations": False,
    },
    "insane": {
        "label": "Insane",
        "description": "Five specials with about four 90+ choices per row.",
        "minRating": 84,
        "highRating": 90,
        "highChoices": 4,
        "specialChoices": 5,
        "featuredChoices": 1,
        "captainMinRating": 90,
        "allowWingbackFormations": False,
    },
}


def normalize_preset(value: Any) -> str:
    name = str(value or "").strip().lower()
    return name if name in PRESETS else DEFAULT_PRESET


def preset_settings(name: Any) -> dict[str, Any]:
    normalized = normalize_preset(name)
    return {"name": normalized, **PRESETS[normalized]}


def load_draft_settings(path: str | None = None) -> dict[str, Any]:
    target = path or SETTINGS_PATH
    try:
        with open(target, "r", encoding="utf-8-sig") as handle:
            payload = json.load(handle)
        requested = payload.get("preset") if isinstance(payload, dict) else payload
    except (OSError, ValueError, TypeError):
        requested = DEFAULT_PRESET
    return preset_settings(requested)


def save_draft_preset(name: Any, path: str | None = None) -> dict[str, Any]:
    normalized = str(name or "").strip().lower()
    if normalized not in PRESETS:
        raise ValueError("Unknown Draft preset: %s" % name)
    target = os.path.abspath(path or SETTINGS_PATH)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    temporary = target + ".tmp"
    with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
        json.dump({"preset": normalized}, handle, indent=2)
        handle.write("\n")
    os.replace(temporary, target)
    return preset_settings(normalized)


def describe(settings: dict[str, Any]) -> str:
    return "%s: %s" % (settings["label"], settings["description"])


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    command = args[0].lower() if args else "show"
    if command in ("show", "current"):
        current = load_draft_settings()
        print("Current Draft preset:", describe(current))
        return 0
    if command in ("list", "presets"):
        for name in PRESETS:
            print("%-8s %s" % (name, describe(preset_settings(name))))
        return 0
    if command == "set" and len(args) >= 2:
        try:
            selected = save_draft_preset(args[1])
        except ValueError as exc:
            print("ERROR:", exc)
            return 2
        print("Draft preset saved:", describe(selected))
        print("Start a new Draft for the new preset to apply to every choice.")
        return 0
    print("Usage: fut_draft_config.py [show|list|set PRESET]")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
