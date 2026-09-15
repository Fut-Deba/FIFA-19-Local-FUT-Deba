#!/usr/bin/env python3
"""Host-side entry handshake for the custom offline FUT Champions run."""

from __future__ import annotations

import json
import tkinter as tk
from tkinter import messagebox
from urllib import error as urllib_error
from urllib import request as urllib_request


STATUS_URL = (
    "http://127.0.0.1:8199/ut/game/fifa19/champion/user/offline-entry"
)
REGISTRATION_URL = (
    "http://127.0.0.1:8199/ut/game/fifa19/champion/user/registration"
)
HTTP_TIMEOUT_SECONDS = 5.0

DIFFICULTIES = (
    (1, "Beginner"),
    (2, "Amateur"),
    (3, "Semi-Pro"),
    (4, "Professional"),
    (5, "World Class"),
    (6, "Legendary"),
    (7, "Ultimate"),
)


def _read_json(url: str, *, method: str = "GET",
               body: dict | None = None,
               timeout: float = HTTP_TIMEOUT_SECONDS) -> dict:
    encoded = (json.dumps(body, separators=(",", ":")).encode("utf-8")
               if body is not None else None)
    request = urllib_request.Request(
        url,
        data=encoded,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib_request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, UnicodeError, ValueError,
            urllib_error.HTTPError, urllib_error.URLError) as error:
        raise RuntimeError("LocalFUT Champions service did not respond") from error
    if not isinstance(payload, dict):
        raise RuntimeError("LocalFUT Champions service returned an invalid reply")
    return payload


def read_offline_entry_status() -> dict:
    """Read the non-mutating entry state owned by the local server."""
    return _read_json(STATUS_URL)


def register_offline_entry(difficulty: int) -> dict:
    """Commit the one explicit enrollment and difficulty transaction."""
    value = int(difficulty)
    if value not in range(1, 8):
        raise ValueError("difficulty must be between 1 and 7")
    return _read_json(
        REGISTRATION_URL,
        method="POST",
        body={"offlineMode": True, "difficulty": value},
    )


def confirm_offline_match(status_reader=read_offline_entry_status) -> dict:
    """Confirm the persisted local run without changing registration state."""
    status = status_reader()
    state = str(status.get("state", "") or "").upper()
    difficulty = int(status.get("difficulty", 0) or 0)
    offline_registered = status.get("offlineRegistered") is True
    event_id = int(status.get("eventId", 0) or 0)
    if (state == "READY_FOR_MATCH" and offline_registered and
            difficulty in range(1, 8) and event_id > 0):
        return {
            "status": "confirmed",
            "eventId": event_id,
            "state": state,
            "difficulty": difficulty,
            "offlineRegistered": True,
        }
    return {
        "status": "refused",
        "reason": "offline-session-not-ready",
        "eventId": event_id,
        "state": state,
        "difficulty": difficulty,
        "offlineRegistered": offline_registered,
    }


def _center_window(window: tk.Tk, width: int, height: int) -> None:
    window.update_idletasks()
    left = max(0, (window.winfo_screenwidth() - width) // 2)
    top = max(0, (window.winfo_screenheight() - height) // 2)
    window.geometry(f"{width}x{height}+{left}+{top}")


def _clear(window: tk.Tk) -> None:
    for child in window.winfo_children():
        child.destroy()


def show_offline_entry_dialog() -> int | None:
    """Ask for enrollment, then one of seven difficulties without a default."""
    root = tk.Tk()
    root.withdraw()
    root.title("FUT Deba Champions")
    root.configure(background="#101a35")
    root.resizable(False, False)
    root.attributes("-topmost", True)
    result: dict[str, int | bool | None] = {
        "confirmed": False,
        "difficulty": None,
    }

    def cancel() -> None:
        result["difficulty"] = None
        root.destroy()

    def choose(value: int) -> None:
        result["difficulty"] = int(value)
        root.destroy()

    def show_difficulties() -> None:
        result["confirmed"] = True
        _clear(root)
        tk.Label(
            root,
            text="CHOOSE YOUR FIXED DIFFICULTY",
            font=("Segoe UI", 19, "bold"),
            foreground="white",
            background="#101a35",
        ).pack(pady=(20, 4))
        tk.Label(
            root,
            text=(
                "This choice applies to all 20 matches and cannot be changed "
                "during the competition.\nTest the difficulty in another "
                "offline mode before confirming.\nRewards increase with both "
                "difficulty and final rank."
            ),
            font=("Segoe UI", 10),
            foreground="#dce7ff",
            background="#101a35",
            justify="center",
        ).pack(pady=(0, 12))
        grid = tk.Frame(root, background="#101a35")
        grid.pack(padx=24)
        for index, (value, label) in enumerate(DIFFICULTIES):
            tk.Button(
                grid,
                text=label,
                command=lambda selected=value: choose(selected),
                width=24,
                height=2,
                font=("Segoe UI", 10, "bold"),
                background="#1f64e8",
                foreground="white",
                activebackground="#16d9ba",
                activeforeground="#081122",
                relief="flat",
            ).grid(row=index // 2, column=index % 2, padx=6, pady=5)
        tk.Button(
            root,
            text="Cancel",
            command=cancel,
            width=18,
            font=("Segoe UI", 10),
        ).pack(pady=(12, 16))
        _center_window(root, 620, 470)

    tk.Label(
        root,
        text="FUT DEBA CHAMPIONS",
        font=("Segoe UI", 22, "bold"),
        foreground="white",
        background="#101a35",
    ).pack(pady=(28, 10))
    tk.Label(
        root,
        text=(
            "Register for a local 20-match competition?\n\n"
            "Matches are played offline against the 20 FUT Deba opponents. "
            "No country, regional leaderboard or online match telemetry is "
            "used."
        ),
        font=("Segoe UI", 11),
        foreground="#dce7ff",
        background="#101a35",
        justify="center",
        wraplength=560,
    ).pack(padx=24, pady=(0, 24))
    buttons = tk.Frame(root, background="#101a35")
    buttons.pack(pady=(4, 26))
    tk.Button(
        buttons,
        text="Register",
        command=show_difficulties,
        width=20,
        height=2,
        font=("Segoe UI", 11, "bold"),
        background="#1f64e8",
        foreground="white",
        activebackground="#16d9ba",
        activeforeground="#081122",
        relief="flat",
    ).grid(row=0, column=0, padx=8)
    tk.Button(
        buttons,
        text="Cancel",
        command=cancel,
        width=20,
        height=2,
        font=("Segoe UI", 11),
    ).grid(row=0, column=1, padx=8)
    root.protocol("WM_DELETE_WINDOW", cancel)
    root.bind("<Escape>", lambda _event: cancel())
    _center_window(root, 640, 360)
    root.deiconify()
    root.lift()
    root.focus_force()
    root.mainloop()
    difficulty = result["difficulty"]
    return int(difficulty) if difficulty is not None else None


def show_offline_entry_error(message: str) -> None:
    """Present one bounded error without leaving FIFA's UI thread waiting."""
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        messagebox.showerror(
            "FUT Deba Champions",
            str(message),
            parent=root,
        )
    finally:
        root.destroy()


def run_offline_entry_flow(
        status_reader=read_offline_entry_status,
        dialog=show_offline_entry_dialog,
        registrar=register_offline_entry) -> dict:
    """Return the guard decision; only the registrar may mutate server state."""
    status = status_reader()
    state = str(status.get("state", "AVAILABLE") or "AVAILABLE").upper()
    difficulty = int(status.get("difficulty", 0) or 0)
    offline_registered = status.get("offlineRegistered") is True
    event_id = int(status.get("eventId", 0) or 0)

    if (state == "READY_FOR_MATCH" and offline_registered and
            difficulty in range(1, 8)):
        return {
            "status": "resume",
            "eventId": event_id,
            "state": state,
            "difficulty": difficulty,
        }
    if state not in ("AVAILABLE", "PICK_DIFFICULTY"):
        return {
            "status": "cancelled",
            "reason": "entry-state-%s" % state.lower(),
        }

    selected = dialog()
    if selected is None:
        return {"status": "cancelled", "reason": "user-cancelled"}
    selected = int(selected)
    if selected not in range(1, 8):
        return {"status": "error", "reason": "invalid-difficulty"}

    receipt = registrar(selected)
    if not (
        receipt.get("success") is True and
        receipt.get("offlineCompetition") is True and
        int(receipt.get("difficulty", 0) or 0) == selected and
        str(receipt.get("state", "")).upper() == "READY_FOR_MATCH"
    ):
        return {"status": "error", "reason": "registration-rejected"}
    return {
        "status": "registered",
        "eventId": int(receipt.get("eventId", event_id) or event_id),
        "state": "READY_FOR_MATCH",
        "difficulty": selected,
    }


def handle_offline_entry_request(payload: dict) -> dict:
    """Run one guard request and always return a reply that releases FIFA."""
    request_id = int((payload or {}).get("requestId", 0) or 0)
    try:
        result = run_offline_entry_flow()
        if result.get("status") == "error":
            show_offline_entry_error(
                "Registration was not completed. You were returned to the "
                "FUT Champions schedule."
            )
    except Exception as error:
        try:
            show_offline_entry_error(
                "The local FUT Champions service is unavailable. You were "
                "returned to the FUT Champions schedule."
            )
        except Exception:
            pass
        result = {"status": "error", "reason": type(error).__name__}
    result["requestId"] = request_id
    return result


def handle_offline_match_request(payload: dict) -> dict:
    """Return one non-mutating receipt that releases the native match hook."""
    request_id = int((payload or {}).get("requestId", 0) or 0)
    try:
        result = confirm_offline_match()
    except Exception as error:
        result = {"status": "error", "reason": type(error).__name__}
    result["requestId"] = request_id
    return result
