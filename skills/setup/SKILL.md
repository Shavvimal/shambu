---
name: setup
description: Installs or updates the shambu CLI (uv tool) and checks the Bambu Studio install, so the /shambu skill can run `shambu` commands. Use when the user runs /shambu:setup or asks to install, update or repair shambu.
disable-model-invocation: true
argument-hint: "[install | update | check]"
allowed-tools: Bash(uv *), Bash(uvx *), Bash(shambu *), Bash(command -v *), Bash(ls *), Bash(defaults read *), Read, AskUserQuestion
---

# shambu setup

Installs the `shambu` command line that the `/shambu` skill drives, then checks the machine. Everything here is
idempotent: run it again to update or repair. `$ARGUMENTS` says which part the user wants: `install` (all steps),
`update` (step 5), `check` (steps 3 and 4); with nothing given, run all steps.

## 1. Preflight

```
command -v uv || echo "uv missing"
ls /Applications/BambuStudio.app/Contents/MacOS/BambuStudio && defaults read /Applications/BambuStudio.app/Contents/Info.plist CFBundleShortVersionString
```

- `uv` missing: install it with `curl -LsSf https://astral.sh/uv/install.sh | sh` (or `brew install uv`), then open a new shell.
- Bambu Studio missing: shambu needs a local Bambu Studio (02.08.x tested). The user installs it from bambulab.com.
  Without it, only `shambu inspect`, `patch`, `recipes`, `spec` and `printer` work.
- Platform: tested on macOS (Apple Silicon). The binary path and config location are macOS paths today.

## 2. Install or update the CLI

```
uv tool install --reinstall "shambu @ git+https://github.com/Shavvimal/shambu"
shambu --version
```

If `shambu` is not found afterwards, run `uv tool update-shell` and open a new shell (the uv tool bin dir must be
on PATH). Alternative without a persistent install: `uvx --from "git+https://github.com/Shavvimal/shambu" shambu doctor`.

## 3. Check the machine

```
shambu doctor
```

Expected: the Bambu Studio version and binary, the profile sources (Application Support copy first, then the app
bundle), the GUI's current presets and bed type, whether a printer is selected and its access code is stored
(the code itself is never printed), the machine's bed and per-extruder reach, and the recipe list. Then:

```
shambu presets list filament --printer "<the machine preset doctor printed>" | head
shambu recipes
```

## 4. Optional: printer status (read-only)

```
shambu printer status
```

shambu reads the printer's serial and LAN access code from Bambu Studio's own configuration, finds the printer on
the LAN, and asks for one status report over MQTT with TLS verified against Bambu's certificate authority. It
never sends a print command. If the printer is not found, pass `--ip <printer-ip>`. If TLS verification fails
on your firmware, `--insecure` skips it; say so to the user because the access code then travels to an unverified
endpoint.

## 5. Updating

After `/plugin update shambu@shambu`, run `uv tool upgrade shambu` (or the install line above) so the CLI matches
the plugin version, then `/reload-plugins`. `shambu doctor` prints the installed version.

## Troubleshooting

- `shambu: command not found`: PATH does not include uv's tool directory; `uv tool update-shell`, new shell.
- `StudioError: Bambu Studio binary not found`: Bambu Studio is not in `/Applications`.
- `PrinterError: no access code stored`: open the printer once in Bambu Studio's Device tab so the code is saved.
- `paho-mqtt missing`: reinstall with the `--reinstall` line above.
