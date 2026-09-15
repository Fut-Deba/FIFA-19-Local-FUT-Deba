# FUT Deba - FIFA 19 Local FUT

**Offline FIFA 19 Ultimate Team, running entirely on your own PC.**

> **Status: early beta.** Things will break. Please read this whole page first.

**Current test release: Beta 1.1.**

FUT Deba brings FIFA 19 Ultimate Team back as a local, offline experience.
Every service runs on your own machine. Nothing is uploaded, there is no
online play and no connection to EA servers.

FUT Deba is a community project. It is not affiliated with, endorsed by or
supported by Electronic Arts. You need your own legitimate copy of FIFA 19 for
PC. This project contains no game files and no way to bypass any copy
protection.

## Contents

1. [Requirements](#1-requirements)
2. [Download](#2-download)
3. [Install and first start](#3-install-and-first-start)
4. [Stopping](#4-stopping)
5. [What works](#5-what-works)
6. [Known issues](#6-known-issues)
7. [Reporting a problem](#7-reporting-a-problem)
8. [What is in this repository](#8-what-is-in-this-repository)
9. [Legal](#9-legal)

## 1. Requirements

- Windows 10 or 11, 64-bit
- A Windows account with administrator rights (you approve one Windows prompt
  each time a session starts)
- A legitimate FIFA 19 PC installation, installed and launched at least once.
  The following exact builds are recognized:

  | Build | Version | Status |
  |---|---|---|
  | FIFA 19 PC EA App update | `19.0.4052077.0` | Supported and validated |
  | FIFA 19 PC 1.0.0.0 | `19.0.3865658.0` | Limited compatibility; exact fingerprint only |

  Other builds, including other v1 variants, are not covered. FUT Deba does
  not provide support for cracked copies, Origin emulators, game DLLs or
  copy-protection bypasses.

### Exact compatible v1 build

The limited v1 compatibility applies only when both installed files match the
following pair. The launcher checks these values automatically before starting:

| File | Size | Product version |
|---|---:|---|
| `FIFA19.exe` | 292,892,672 bytes (279.32 MiB) | `19.0.3865658.0` |
| `CardsDLL_Win64_retail.dll` | 4,481,856 bytes (4.27 MiB) | `19.0.3865658.0` |

Launch strategy: `legacy-native-server`. This exact pair is known to work, but
broader v1 feature parity is not claimed.

- An Internet connection for the first start only, to install the runtime

## 2. Download

Download the ZIP from the **Releases** page of this repository, not the
"Source code" archives. The source in this repository does not include the game
data the local server needs, so only the release ZIP can be played.

## 3. Install and first start

1. Create a new empty folder, for example `Desktop\FutDeba-Beta-1.1`, and
   extract the complete ZIP into it. Do not select Desktop or Downloads itself
   as the destination, and do not run anything from inside the ZIP.
2. Run **`FUT_DEBA_LAUNCHER.cmd`**. This is the only file you need.
3. The first time, a setup window installs everything by itself and ends with
   `SETUP VERIFIED`. The launcher then opens. Later starts open the launcher
   directly.
4. On the launcher's **GAME** page, check that your FIFA 19 was detected.
5. Close FIFA 19 if it is open, press **START LOCAL FUT** and approve the
   Windows prompt.
6. Wait until the launcher says you can enter FUT, then enter Ultimate Team
   from the game menu. The 1.0.0.0 build keeps the loading state for 30 extra
   seconds before the launcher reports that the local session is ready.

### If FIFA 19 is not found

Automatic detection looks in:

- Program Files (EA Games, Origin Games)
- your Desktop and Games folders
- on every drive: the Games, EA Games, Origin Games, Steam, Epic Games and
  XboxGames folders, up to three folders deep

If your game is somewhere else, move it into one of those folders, or pick it
once with **BROWSE** on the GAME page. The launcher remembers it.

### If START LOCAL FUT fails

The launcher shows the reason on its **HOME** page.

If it says FUT Deba cannot share its local ports with another program, that
program is usually another FUT revival tool that is still running. Close it
completely, or restart the PC, then press START LOCAL FUT again.

For any other failure, take a screenshot of the reason and report it (see
[Reporting a problem](#7-reporting-a-problem)) together with the file
`launcher-action.log` from `%LOCALAPPDATA%\FIFA19LocalFUT`.

## 4. Stopping

Press **STOP LOCAL FUT** in the launcher, or close FIFA 19 normally. The game
closes within about 20 seconds and the launcher restores your Windows settings
on its own. Finish or leave any match first: an unfinished match is lost.

## 5. What works

- Ultimate Team onboarding: club, kit and badge selection, starter packs
- Store and pack opening, including player picks
- Squad Building Challenges
- Transfer Market: buy, list and sell players and items
- FUT Draft (single player)
- Squad Battles, including rewards
- Team of the Week challenges
- Single Player Seasons: enter a competition, play, return to the menus,
  finish with promotion or relegation, or forfeit the season
- Daily and weekly objectives, reset every day and every week
- A separate Road to Glory account, kept apart from your normal club

## 6. Known issues

These are known. You do not need to report them.

- Season competition cards show a code such as `*SEASON_LOC_19010` instead of
  the competition name, and a generic trophy.
- After a Team of the Week challenge, the Squad Battles opponent can be named
  "TOTW" plus a number for the rest of that session.
- FUT Champions is disabled.
- Returning from a Buy Now inside an SBC player search can still misbehave.
- This is a local recreation, not EA's live service: some screens show
  placeholder text or artwork.
- FUT Deba cannot run at the same time as another FUT revival for any FIFA
  (18, 20, 21, 22 and similar). They use the same EA address
  (`spring18.gosredirector.ea.com`) and the same local ports (42230, 10051,
  8199). Fully close the other tool before START. Its redirect lines are set
  aside while a FUT Deba session runs and put back when it ends, so the other
  tool keeps working afterwards.

## 7. Reporting a problem

Open an issue on this repository and include:

- what you were doing, and what you expected instead
- the time on your clock when it happened, to the minute
- a screenshot, if the problem is visible
- your FIFA 19 build (shown on the launcher's GAME page)
- only the relevant final log lines, with personal paths and session data
  removed

If the game freezes, leave it frozen for two full minutes before closing it.
Logs can contain machine paths or session metadata. Do not attach complete
diagnostic files to a public issue; share them privately only when a maintainer
requests them. Never attach game files, account databases, certificates or
keys.

A bug report template is in `ADVANCED\BUG_REPORT_TEMPLATE.md`.

## 8. What is in this repository

| Path | What it is |
|---|---|
| `FUT_DEBA_LAUNCHER.cmd` | Start here |
| `0_READ_INSTRUCTIONS_FIRST.md` | The instructions shipped inside the release |
| `ADVANCED\` | Tools most players never need, the test checklist and the bug report template |
| `LocalFUT\server\` | The local FUT, Blaze and redirector server |
| `LocalFUT\tools\` | Launcher, build detection and game guards |
| `LEGAL_NOTICE.txt` | Read before using or sharing |

Game data extracted from FIFA 19 and content derived from third-party sites is
not part of this repository and is never committed.

## 9. Legal

FIFA, FIFA Ultimate Team and EA are trademarks of Electronic Arts Inc. FUT Deba
is an independent, non-commercial community project with no affiliation to
Electronic Arts. Use it only with a copy of FIFA 19 you own. See
[`LEGAL_NOTICE.txt`](LEGAL_NOTICE.txt).
