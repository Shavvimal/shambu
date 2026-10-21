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
uv tool install --reinstall "shambu @ git+https://github.com/Shavvimal/shambu@v0.4.0"
shambu --version        # must print: shambu 0.4.0
```

The tag matches this setup text, and the `/shambu` skill text expects the same version. If `shambu` is not found
afterwards, run `uv tool update-shell` and open a new shell (the uv tool bin dir must be on PATH). Alternative
without a persistent install: `uvx --from "git+https://github.com/Shavvimal/shambu@v0.4.0" shambu doctor --expect-version 0.4.0`.

Install from the tag above, not from a local checkout: `uv tool install .` copies the code once and does not follow
the plugin (that is how a 0.2.1 CLI ran under a 0.3.0 plugin). A checkout is for development only.

## 3. Check the machine

```
shambu doctor --expect-version 0.4.0
```

Exit 3 with `skill expects 0.4.0: MISMATCH` means step 2 did not take: run it again and open a new shell. Expected:
the shambu version and how it was installed, the Bambu Studio version and binary, the profile sources (Application Support copy first, then the app
bundle), the GUI's current presets and bed type, whether a printer is selected and its access code is stored
(the code itself is never printed), the machine's bed and per-extruder reach, the spools loaded on the printer
(`loaded on the printer (read HH:MM ...)`, one line per nozzle with the resolved preset name, and a `MISMATCH`
line when the GUI's last-used filament differs), and the recipe list. `loaded on the printer: not read (...)`
means the printer was not reachable: pass `--ip <printer-ip>` or check it is on. Then:

```
shambu presets list filament --printer "<the machine preset doctor printed>" | head
shambu recipes
```

## 4. Printer read (read-only)

```
shambu printer filaments
shambu printer status
```

shambu reads the printer's serial and LAN access code from Bambu Studio's own configuration, finds the printer on
the LAN, and asks for one status report over MQTT with TLS verified against Bambu's certificate authority.
`filaments` lists the spool in each nozzle with its resolved preset name; `check` and `build` read the same
report before every build, so this step confirms the read works. It never sends a print command. If the printer is not found, pass `--ip <printer-ip>`. If TLS verification fails
on your firmware, `--insecure` skips it; say so to the user because the access code then travels to an unverified
endpoint.

## 5. Updating

1. `/plugin marketplace update shambu` (refreshes the local copy of the repo; without it, `/plugin update` finds nothing new).
2. `/plugin update shambu@shambu`, then `/reload-plugins`.
3. Run `/shambu:setup install` again: the new setup text pins the matching tag in step 2. Not `uv tool upgrade
   shambu`: it keeps the old source (a tag or a local folder). The line it runs is
   `uv tool install --reinstall "shambu @ git+https://github.com/Shavvimal/shambu@vX.Y.Z"` with the new version.
4. `shambu doctor --expect-version 0.4.0` exits 0.

## Troubleshooting

- `shambu: command not found`: PATH does not include uv's tool directory; `uv tool update-shell`, new shell.
- `StudioError: Bambu Studio binary not found`: Bambu Studio is not in `/Applications`.
- `PrinterError: no access code stored`: open the printer once in Bambu Studio's Device tab so the code is saved.
- `paho-mqtt missing`: reinstall with the `--reinstall` line above.
- `PresetError: filament preset 'loaded' not found`: the CLI is older than 0.3.0. Run step 2.
- `doctor` exit 3 with `MISMATCH`: the CLI and the plugin differ. Run step 2; if `shambu --version` still shows the
  old number, `which -a shambu` lists the other copy on PATH.
