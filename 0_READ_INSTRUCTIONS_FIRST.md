# FIFA 19 Local FUT — Beta 1.1

This package is intended for users who own a legitimate FIFA 19 PC
installation. It contains no game executable, DLL, activation material, or
method for bypassing DRM.

## If Windows blocks a file

Windows tags every file that arrived from the internet. Smart App Control
refuses to run a tagged script regardless of what is inside it, so this is
Windows objecting to where the file came from, not to the package.

The launcher's first-time setup clears that tag from the whole package as its
first step, so you should see this at most once, on `FUT_DEBA_LAUNCHER.cmd`.

If Windows does block it, clear the tag on the **ZIP** before extracting:
right click the ZIP, choose **Properties**, tick **Unblock** at the bottom,
press OK, then extract. Everything extracted is then untagged and setup runs
normally.

**Do not turn off Smart App Control, Microsoft Defender or SmartScreen, and
do not add antivirus exclusions.** Nothing in this package needs any of that,
and turning Smart App Control off can require a Windows reset to undo. If a
file is blocked, send the exact Windows message instead.

## First-time setup

1. Extract the complete ZIP to a normal folder. Do not run files from inside
   the ZIP.
2. Run `FUT_DEBA_LAUNCHER.cmd`. The first time, it prepares everything by
   itself: it installs the isolated dependencies and verifies every package
   file, then opens the launcher. Later starts open the launcher directly.
3. On the launcher's Game page, confirm the detected FIFA 19 build, or use
   AUTO-DETECT or BROWSE if it was not found.

The `ADVANCED` folder holds tools most testers never need: a session without
the launcher window, removal of redirects left by an older LocalFUT19
version, the separate Road to Glory account, the test checklist and the bug
report template.

## Recognized builds

The detector verifies the complete `FIFA19.exe` and
`CardsDLL_Win64_retail.dll` pair automatically:

- `19.0.3865658.0` — recognized v1 profile with limited compatibility;
- `19.0.4052077.0` — the supported and validated EA App target.

Only the exact v1 executable and CardsDLL pair fingerprinted by the launcher
is covered. Other v1 releases have not completed the compatibility matrix and
are unsupported.

Exact compatible v1 pair:

- `FIFA19.exe` — 292,892,672 bytes (279.32 MiB), product version
  `19.0.3865658.0`;
- `CardsDLL_Win64_retail.dll` — 4,481,856 bytes (4.27 MiB), product version
  `19.0.3865658.0`;
- launch strategy: `legacy-native-server`.

The launcher verifies the exact compatible pair automatically.

When both builds are installed, the launcher automatically prefers the newer
EA App build. A mixed, modified, or unknown pair is rejected before the package
changes `hosts`, starts services, or attaches to the process.

The launcher detects installed builds automatically, and BROWSE can select an
installation in another folder.

## Starting and stopping a session

Run `FUT_DEBA_LAUNCHER.cmd`, confirm the detected build and account, then use
`START LOCAL FUT`. The launcher keeps game configuration, account tools and
diagnostics in one place. `ADVANCED\PLAY_WITHOUT_LAUNCHER.cmd` remains the
direct recovery entry point for this prototype.

The launcher applies these rules:

- v1 prepares the server and temporary redirects, starts FIFA, and restores
  `hosts` byte for byte when the game exits;
- EA App starts FIFA through its official activation path, waits for a stable
  and network-idle main-menu process, and then starts the guarded local bridge.
  No manual Enter step is required.

Do not start FIFA or a separate server manually. Close FIFA normally when the
session is finished and allow the launcher to restore `hosts` and stop its
local services.

FUT Deba cannot run at the same time as another FUT revival tool, for FIFA 19
or for a later FIFA: they use the same EA address and local ports. Close the
other tool completely before `START LOCAL FUT`; if it still holds a port, the
launcher names it before FIFA starts. Redirect lines the other tool left in
`hosts` are set aside for the session and put back when it ends. An unverified FIFA 19 build is displayed with its fingerprint
and a clear compatibility warning, but Play remains disabled.

`ADVANCED\RTG_ACCOUNT_MODE` contains the optional launcher for a separate Road to Glory
account. On the EA App build it never shares a database with the Normal
account, and its protected reset command creates a backup first. The v1 RTG
profile is outside this beta's validation scope.

## State, logs, and privacy

Runtime data is stored under `%LOCALAPPDATA%\FIFA19LocalFUT`. The v1 and EA App
profiles use separate database locations. Certificates, recovery snapshots,
and logs are never written into the repository or game directory.

Use `ADVANCED\BUG_REPORT_TEMPLATE.md` for reports. Never submit an account database,
certificate, credential, game file, or raw network payload.

## Beta limitations

- the EA App build above is the fully supported target;
- v1 compatibility is limited to the exact `19.0.3865658.0` fingerprint;
- no public FUT service or matchmaking is provided; every service is loopback
  only;
- a public release still requires clean-PC validation and every gate listed in
  `ADVANCED\TEST_CHECKLIST.md`.

Read `COPYRIGHT.txt` and `LEGAL_NOTICE.txt` before using or sharing this package.
