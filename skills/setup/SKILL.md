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
uv tool install --reinstall "shambu @ git+https://github.com/Shavvimal/shambu@v0.5.1"
shambu --version        # must print: shambu 0.5.1
```

The tag matches this setup text, and the `/shambu` skill text expects the same version. If `shambu` is not found
afterwards, run `uv tool update-shell` and open a new shell (the uv tool bin dir must be on PATH). Alternative
without a persistent install: `uvx --from "git+https://github.com/Shavvimal/shambu@v0.5.1" shambu doctor --expect-version 0.5.1`.

Install from the tag above, not from a local checkout: `uv tool install .` copies the code once and does not follow
the plugin (that is how a 0.2.1 CLI ran under a 0.3.0 plugin). A checkout is for development only.

## 3. Check the machine

```
shambu --version                        # must print: shambu 0.5.1
shambu doctor --expect-version 0.5.1
```

Run `shambu --version` first: every release prints it. If it shows another number, step 2 did not take: run it
again and open a new shell. A usage error on `--expect-version` (`unrecognized arguments`) means the installed CLI
predates 0.4.0: run step 2. Exit 3 with `skill expects 0.5.1: MISMATCH` means the same. Expected: the shambu version
and how it was installed, the Bambu Studio version and binary, the profile sources (Application Support copy first,
then the app bundle), the GUI's current presets and bed type, whether a printer is selected and its access code is
stored (the code itself is never printed), with a profile the line `printer profile <name> (default): serial
<masked>, mode lan|cloud|unknown, last login HH:MM UTC, TLS verified|TLS check off (--insecure), code:
stored|none|unreadable; N profile(s) in <config dir>`, the machine's
bed and per-extruder reach, the spools loaded on the printer (`loaded on the printer (read HH:MM ...)`, one line per
nozzle with the resolved preset name, and a `MISMATCH` line when the GUI's last-used filament differs), and the
recipe list. `loaded on the printer: not read (...)` means the printer was not read: check it is on, pass `--ip
<printer-ip>`, or run step 4. Then:

```
shambu presets list filament --printer "<the machine preset doctor printed>" | head
shambu recipes
```

## 4. Printer login (read-only)

```
shambu printer login --ip <printer-ip> --serial <serial> --mode lan   # the code is prompted, not shown
shambu printer login --from-studio                                    # or: Studio's selected printer and its code
shambu printer status
```

Never ask for the access code in chat. The user runs the first `login` line in their own terminal (the prompt hides
the code), with the IP and the serial from the printer's touchscreen and `--mode lan` or `--mode cloud` as the
touchscreen shows, or passes `--access-code-file FILE` with a file they wrote themselves; never put the code on the
command line. If the user pastes a code in chat, do not repeat it or use it: ask them to run `login` themselves.
`login` reads the printer once with the code and, only when that read succeeds, stores a profile in
`printers.json` and the code in `credentials.json` (mode 0600) in shambu's config dir (`$SHAMBU_CONFIG_DIR`, else
`$XDG_CONFIG_HOME/shambu`, else `~/.config/shambu`). It prints `profile <name>: serial <masked>, code stored, mode lan, read at HH:MM UTC`.
`shambu printer list` shows the profiles. After that, every command that reads the printer uses the default profile,
or the one `--printer NAME` names; the code comes from a one-shot flag, then the stored code, then Studio's config.
Without `--serial`, `login` takes the serial from the printer's certificate; the serial from the touchscreen is
the stronger check that the profile names the intended printer.

A read through a profile goes to the IP the profile stores and does not search; `login` always stores the IP it read
from. The subnet pass (the default-route /24 after the ARP table) runs in `printer discover` (always), in `login
--from-studio` without `--ip`, and on a profile read only when the profile has no IP. When the printer's address
changes, a read fails with `cannot connect ... The profile NAME expected this address (last login ...)`: run
`shambu printer discover`, then `shambu printer login --printer NAME --ip <ip>`. Do not guess the cause: the printer
can also be off.

LAN-only mode and cloud mode use the same local read (TLS on port 8883, one status request); the mode changes only
where a new code comes from. The printer can regenerate its code when LAN-only mode is toggled, so after a toggle
run `login` again with the code from the touchscreen (LAN-only), or with `--from-studio` after Studio has reconnected
(cloud).

`status` and `filaments` confirm the read; `check` and `build` read the same report before every build. shambu
never sends a print command. If TLS verification fails on your firmware, `--insecure` skips it (for a new printer,
`login --insecure` needs `--serial`); say so to the user because the access code then travels to an unverified
endpoint. `login --insecure` stores that choice on the profile, and every later read through the profile skips the
check; a `login --printer NAME` refresh without `--insecure` clears it.

## 5. Updating

1. `/plugin marketplace update shambu`: it refreshes the local copy of the repo and bumps the installed plugin to the
   marketplace version in one step (`Updated 1 marketplace (1 plugin bumped)`).
2. `/reload-plugins`. Only when step 1 reports no bump, run `/plugin update shambu@shambu` and reload again.
3. Run `/shambu:setup install` again: the new setup text pins the matching tag in step 2. Not `uv tool upgrade
   shambu`: it keeps the old source (a tag or a local folder). The line it runs is
   `uv tool install --reinstall "shambu @ git+https://github.com/Shavvimal/shambu@vX.Y.Z"` with the new version.
4. `shambu doctor --expect-version 0.5.1` exits 0.

## Troubleshooting

- `shambu: command not found`: PATH does not include uv's tool directory; `uv tool update-shell`, new shell.
- `StudioError: Bambu Studio binary not found`: Bambu Studio is not in `/Applications`.
- `ConfError: no access code for serial <masked>`: run `shambu printer login` (step 4), or open the printer once in
  Bambu Studio's Device tab so the code is saved.
- `refused the MQTT login`: do not state a cause and do not retry, because the printer also refuses while it boots
  or shuts down. Quote the error's `source:` and its next step, then hand back: read the code off the touchscreen
  and run `shambu printer login --printer NAME` (cloud mode: open Studio once, then `login --from-studio`).
- `cannot connect ... The profile NAME expected this address`: run `shambu printer discover`, then `shambu printer
  login --printer NAME --ip <ip>` with the address it finds.
- `paho-mqtt missing`: reinstall with the `--reinstall` line above.
- `PresetError: filament preset 'loaded' not found`: the CLI is older than 0.3.0. Run step 2.
- `unrecognized arguments: --expect-version`: the CLI is older than 0.4.0. Run step 2.
- `doctor` exit 3 with `MISMATCH`: the CLI and the plugin differ. Run step 2; if `shambu --version` still shows the
  old number, `which -a shambu` lists the other copy on PATH.
