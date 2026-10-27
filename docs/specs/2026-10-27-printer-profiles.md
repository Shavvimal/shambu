# Feature spec: printer profiles for LAN-only mode and cloud mode (2026-10-27)

Status: IMPLEMENTED in 0.5.0 with the section 11 decisions; section 11 supersedes every Keychain, `security`, `code_source`, relocation and Application Support passage below. Severity of the gap: medium (the tool reads the printer only through Bambu Studio's config plus an ARP scan; the day the printer went to LAN-only mode, every read failed until the owner's assistant hand-edited Studio's config with Studio closed). The owner's words: "I need to enable LAN access, what does that mean for me?" and "keep in mind I want to use Account mode (Bambu Cloud) as well as LAN mode. Both should be supported." Written up from the 2026-10-27 session and three research passes over `src/shambu/printer.py`, `src/shambu/conf.py`, the Studio source (master `79df7ae`, 13 Oct 2026), ha-bambulab (`0e027ff`, 21 Sep 2026) and the community protocol docs. Nothing in this document is implemented.

## 1. What happened, in order

1. 2026-10-27, morning: the owner switched the X2D to LAN-only mode on the touchscreen so a teammate can develop against it. The printer regenerated its LAN access code (one X2D observation, n=1; sources conflict on whether the toggle always does this, section 8).
2. `shambu doctor` reported `loaded on the printer: not read (PrinterError: no host on this LAN answers on port 8883. Is the printer on and on this network? Pass --ip.)`. `shambu printer discover` (ARP plus a port probe, no SSDP by default) found nothing, because the ARP table on this Mac had no entry for the printer.
3. A hand scan of the subnet for port 8883 found the printer. `shambu printer status --ip <ip>` then failed with `printer <serial> at <ip> refused the MQTT login (Not authorized): check the LAN access code in Studio`. Studio's stored code was the old one.
4. shambu has no way to take a new access code. The assistant backed up `BambuStudio.conf`, wrote the new code under `access_code.<serial>` while Studio was closed, and the read worked. This is exactly the write that `conf.py`'s docstring forbids callers to do, and it survives only until Studio next saves its config (Studio writes the whole file every 5 s while dirty and again on exit, `GUI_App.cpp:2999-3000,3716-3721`; after a refused login it clears the stored code, `GUI_App.cpp:2280-2290`).
5. Every later command needs `--ip`, and nothing remembers it. The session memory outside the repo now holds the IP, which is the wrong place.

## 2. Root cause

1. **Credentials have one source.** `PrinterService.resolve()` (`printer.py:1185-1190`) is hard-wired to `conf.load()`, `conf.selected_serial()` and `conf.access_code()`. Studio's config is right only while Studio itself is bound to the printer in the same mode. In LAN-only mode Studio needs a fresh bind in its Device tab before its config is useful again, and in cloud mode the code is whatever the cloud bind list last said (`DevManager.cpp:883-887`).
2. **The IP has no home.** Studio stores the IP of a LAN-mode printer encoded against `slicer_uuid` (`user_access_dev_ip.<serial>`, `DeviceManager.cpp:518-535`), and nothing for a cloud printer. shambu stores nothing and rediscovers on every call. The default discovery is `ArpProbe` only (`printer.py:1115`): it probes port 8883 on the hosts already in the ARP table, so a printer this Mac has not talked to recently is invisible. `SsdpListen` exists (`printer.py:278-341`) and even parses `DevConnect` (lan or cloud), but only `printer discover --ssdp N` runs it, and nothing reads the mode field.
3. **The error text points at Studio.** `printer.py:560`, `:1189`, `:1209`, `:1323`, `:1328` and `conf.py:65`, `:68` all tell the user to fix Studio. For a LAN-only printer that advice is a detour, and for a rotated code it is wrong: Studio clears the code it held.
4. **Mode is invisible.** Nothing records or reports whether the printer is in cloud mode or LAN-only mode, so the hand-back cannot say why a read failed or where a fresh code would come from.

## 3. What the tool and the printer already offer, unused

- **One transport serves both modes.** The local broker on port 8883 accepts `bblp` plus the access code in cloud mode as well as LAN-only mode. shambu read the X2D on 2026-10-16 while the job reported `print_type: cloud` (`skills/shambu/references/04-app-printer-control.md:412`), and the 2026-10-27 read was in LAN-only mode. Status pushes are outside Authorization Control by Bambu's own list (the 16 Jan 2025 firmware post), so neither mode needs Developer Mode for a read. The mode changes only where the code comes from and how the printer is found.
- **The TLS certificate names the printer.** The broker's certificate has the serial as its CN, and `check_identity` (`printer.py:351-357`) already compares it. A TLS handshake with hostname checks off, and no MQTT CONNECT, reveals the serial of whatever answers at an IP. That gives `login --ip` the serial without the touchscreen.
- **A refused login is one CONNACK code.** `MqttReader` already maps "Not authorized" to a `PrinterError` (`printer.py:560`). ha-bambulab treats the same code as "the access code rotated" and stops, because the printer also returns it while it boots or powers down (ha-bambulab issue #1863).
- **A data-file pattern exists.** `calibration.json` has a version field, an exact key set, a validator on read and write, an atomic `mkstemp` plus `os.replace`, an `fcntl.flock` lock for load-change-save and a `path=` override for tests (`conf.py:112-286`). A profile store copies it.
- **SSDP carries the mode.** A NOTIFY packet holds `DevConnect.bambu.com: lan|cloud` and `DevBind.bambu.com: free|occupied` and needs no login (`DevManager.cpp:126-143`, the community packet captures). `SsdpListen` parses it today.

## 4. Required behaviour

1. **A shambu-owned profile store** at `~/Library/Application Support/shambu/printers.json`, beside `calibration.json`. One entry per printer: `name`, `serial`, `ip`, `model`, `mode` (`lan`, `cloud` or `unknown`), `code_source` (`keychain` or `studio`), `added` and `verified` (the UTC time of the last successful read). No access code ever goes in this file. JSON, not TOML: the package has no TOML writer and adds no dependency for this.
2. **The access code lives in the macOS Keychain**, service `shambu`, account = the serial, written and read by `/usr/bin/security` so the item's ACL trusts that one binary and no prompt appears whichever Python runs shambu. The write goes through `security -i` on stdin, never `-w <code>` on argv. The read is `find-generic-password -w`; exit 44 means "no item".
3. **Both modes, one path.** The read stays local MQTT, `bblp`, one `pushall`, in both modes. The profile's `mode` is recorded, not configured: from `DevConnect` when a NOTIFY is heard, else from what `login` observed, else `unknown`. The mode changes three things: the hint in a refused-login message (section 5.4), whether the Studio config is a trusted code source (cloud: yes, Studio refreshes it from the account; LAN-only: only after a fresh Device-tab bind), and what `doctor` prints.
4. **Resolution order for the IP:** `--ip`, then the selected profile's `ip`, then the 8883 subnet probe, then SSDP. When the profile's IP does not answer on 8883 (a connect failure, not a refusal), the probe runs, and a hit whose certificate serial matches the profile updates the profile's `ip` and prints one line saying so. A hit with another serial is reported and not stored.
5. **Resolution order for the code:** `--access-code-stdin` or `--access-code-file` (one-shot, never stored unless the verb is `login`), then the Keychain item for the serial, then Studio's `access_code.<serial>` as today. The source used is named in `doctor` and in every refused-login message.
6. **Resolution order for the serial:** `--serial`, then the selected profile, then Studio's selected printer, then the certificate CN of the host at `--ip`.
7. **The 8883 subnet probe replaces the ARP-only probe** as the default finder: ARP hosts first (they are cheap and usually right), then the rest of the /24 that the Mac's primary interface is on, 64 threads, 0.5 s per host, inside the existing discovery deadline. A /24 at 0.5 s and 64 threads takes about 2 s. Larger subnets are not scanned; the user passes `--ip` or runs `login`.
8. **No profile, no change.** With no `printers.json`, every command behaves as 0.4.0 does. A profile is opt-in through `login`.
9. **Studio's config is never written.** `login --from-studio` copies Studio's selected serial and code into a profile and the Keychain. Nothing copies the other way: Studio rewrites its file every 5 s while running and clears the code after a refused login, so a write from shambu would be lost or would mislead.
10. **Cloud MQTT and the Bambu account API are out of scope** for 0.5.0. They need an account token that lasts about 90 days, a Cloudflare-fingerprint workaround, and the login endpoint broke in May 2026 (ha-bambulab issue #1999). SECURITY.md promises no network call other than the printer. A later phase can add a single bind-list call to refresh the code after a refusal, behind an explicit flag; section 8 keeps the question.

## 5. The verbs and flags

### 5.1 `shambu printer login`

```
shambu printer login --ip 192.0.2.40                       # prompts for the code on the tty, hidden
shambu printer login --ip 192.0.2.40 --access-code-stdin < code.txt
shambu printer login --ip 192.0.2.40 --serial <serial> --name x2d
shambu printer login --from-studio [--name x2d]            # seed from Studio's selected printer and its stored code
shambu printer login --ip 192.0.2.40 --insecure            # as today: TLS unverified, say so
```

Steps, in order, each a timed step like today's `credentials`, `arp`, `probe`, `mqtt`: (1) TLS handshake to `<ip>:8883` with hostname checks off, read the certificate CN; with `--serial` given, a mismatch is a FAIL; without it, the CN becomes the serial. (2) Read the code: `--access-code-stdin`, `--access-code-file`, `--from-studio`, or a hidden tty prompt; with no tty and no source, usage error, exit 2. (3) One verified MQTT read: connect, subscribe, one `pushall`, wait for a full report, as `MqttReader.read` does today. (4) Only on success: write the Keychain item, then the profile with `verified` set, `mode` from a passive SSDP listen of at most 3 s if one was heard, else `unknown`. (5) Print the hand-back line: `profile x2d: serial <masked>, code stored in the Keychain, mode lan, read at HH:MM`. A refused login writes nothing and exits 3 with the section 5.4 text.

`--name` defaults to the lowercased model name (`x2d`) and must be unique; a second `login` for the same serial updates that profile whatever its name. The first profile becomes the default. `--default` moves the default.

### 5.2 `shambu printer list`, `shambu printer forget`

`list` prints one line per profile: name, masked serial, IP, mode, code source, `verified` time, and `(default)`. This is the one place the IP appears on stdout, because the user asked for the printer's details; `status`, `watch`, the logs and the hand-back keep the 0.4.0 rule and never show it. `--json` gives one document with the same fields and no code. `forget NAME` removes the profile and its Keychain item (`security delete-generic-password`), asks for nothing, exits 0, and reports what it removed; `--keep-code` leaves the Keychain item.

### 5.3 `--printer NAME` on every printer-reading command

`doctor`, `check`, `build`, `sweep` and `printer *` take `--printer NAME`. Without it the default profile applies. `--ip`, `--serial` and `--no-printer` keep their meaning and win over the profile. `_printer_flags` (`cli.py:135-138`) and `Printer.configure` (`cli.py:1161-1185`) both gain the flag; `Bambu.printer()` (`facade.py:318-329`) applies the selected profile's `ip`, `serial` and, when the profile recorded it, `insecure`.

### 5.4 Error text

The refused-login message becomes, in LAN-only mode: `printer <masked> at <ip> refused the access code (source: Keychain). The printer regenerates it when LAN-only mode is toggled, when the refresh icon next to the code is pressed, after some firmware updates, and sometimes after a reboot. Read the code off the touchscreen and run: shambu printer login --printer NAME`. In cloud mode the last sentence is: `Open Bambu Studio once so it fetches the current code from your account, then run: shambu printer login --from-studio --printer NAME`. In mode `unknown`, both sentences. No retry on a refusal: the printer also refuses while it boots or shuts down.

`no host on this LAN answers on port 8883` keeps its text and adds: `The profile NAME expected <ip> (last read HH:MM).` `conf.py:68` ("open the printer once in Studio's Device tab") becomes `no access code for serial <masked>: run shambu printer login, or open the printer once in Bambu Studio's Device tab`. The `watch` category table (`printer.py:1320-1341`) gets a `code` category for refusals that names `login`, and its `setup` category stops naming Studio alone.

### 5.5 `doctor`

The config line `selected printer <masked>, access code stored: True` becomes two lines: `printer profile x2d (default): serial <masked>, mode lan (SSDP 10:57 | stored | unknown), code from Keychain, last read HH:MM` and, when Studio also holds a code, `Studio: selected printer <masked>, access code stored: True`. The printer section's `not read (...)` line names the source it tried, for example `not read (PrinterError: ... (source: Keychain, profile x2d))`. The timings line gains `printer.profile` and `printer.keychain`.

### 5.6 `shambu printer probe`

The verb reference 04 planned (`04-app-printer-control.md:362`) and never shipped. It runs steps 1 and 3 of `login` against the resolved printer and prints: IP source, serial source, code source, TLS verified or not, `DevConnect` if a NOTIFY arrived within 3 s, CONNACK result, and the `print.fun` signature bit (`0x20000000`, which ha-bambulab reads as "MQTT signature required"). It stores nothing. It is the one-command answer to "which mode is the printer in and does my code work". Exit 0 on a full report, 3 otherwise.

## 6. Design decisions and the ones rejected

- **JSON over TOML.** `tomllib` reads only. A writer is a dependency or a hand serialiser. The calibration file is JSON. Decided: JSON.
- **`security` subprocess over the `keyring` library.** `keyring` makes the calling Python the item's creator, so a Python upgrade or a new venv brings "allow / always allow" prompts. `/usr/bin/security` is stable. Decided: `security`, with an injectable runner like `ArpProbe.runner` so Linux CI fakes it. The REVIEW-RUBRIC item 2 list of modules that may call `subprocess.` grows by the new module.
- **A new module `vault.py`** holds the Keychain read, write and delete, and the stdin protocol, with a docstring that states it handles the access code at rest and nothing else. `conf.py` keeps the profile file I/O (`load_profiles`, `save_profiles`, `update_profiles`) and a pure `profiles.py` holds the schema and validator, the same split as `calibrate.py` and `conf.py`.
- **Code never in `printers.json`.** The file is 0600 by construction (`mkstemp`), but the SECURITY.md promise is "never written". Decided: Keychain only. On Linux or Windows, where `security` does not exist, `login` refuses with "no secure store on this platform; pass `--access-code-file` per command" and stores the profile without `code_source`.
- **Reading Studio's config stays as the third source**, not an opt-in as reference 04 line 350 proposed, because 0.4.0 users rely on it and in cloud mode it is the freshest source there is. Decided: keep, but name the source on every use.
- **No `--also-studio`.** The earlier idea of pushing the code into Studio's config so its Device tab works without a second entry is dropped: Studio holds its config in memory and rewrites the whole file every 5 s while running, and on macOS it checks no trailer, so the write only works with Studio closed and only until the next refused login clears it. Studio's own Device tab takes the IP and code in one dialog. Decided: not done.
- **Scan the /24, not the ARP table only.** The ARP table had no printer entry on 2026-10-27. A /24 at 64 threads and 0.5 s is about 2 s. Decided: ARP hosts first, then the rest of the /24 of the primary interface, inside the existing deadline, nothing larger.
- **SSDP stays passive and bounded.** It gives the mode for free but arrives at most every 1800 s (`Cache-Control: max-age=1800` in the NOTIFY) and not across VLANs. Decided: a 3 s listen inside `login` and `probe`, `--ssdp N` elsewhere as today, and `mode: unknown` when nothing arrives.
- **`get_access_code` over MQTT is never sent.** Studio sends `{"system":{"command":"get_access_code"}}` on every connect (`DeviceManager.cpp:1267,2632`) and the printer answers with the code. It would make a rotated code self-healing. It is not `pushall`, so the read-only rule forbids it. Decided: never.

## 7. Acceptance (testable)

- `printers.json` round-trips with `version: 1`, refuses an unknown key, a missing key, an identifying value in `name`, or a `mode` outside `lan|cloud|unknown`, and writes nothing on refusal (mirrors `tests/test_calibrate.py:338-419`).
- A write failure is a `DataFileError`, leaves no temp file, and exits 3 through `cli.main`.
- Two threads running `update_profiles` both land (the lock test from `test_calibrate.py:389-409`).
- The autouse `offline` fixture (`tests/conftest.py:24-35`) points `conf.PRINTERS` at a missing file and replaces `vault` with an in-memory fake, so no test touches the real Keychain or `~/Library`.
- A sentinel code from the fake Keychain never appears in stdout, stderr, `--json` output, `printers.json`, the watch log or any `PrinterError` text (extends `test_cli_printer_status_never_prints_code` and `test_public_raw_scrubs_a_known_access_code_under_an_ordinary_key`).
- `conf.sensitive_values()` includes every profile serial, its mask and every Keychain code the process has read, so `scrub_text`, `scrub_tree` and `public_raw` catch them.
- The code reaches `security` on stdin: a test captures the fake runner's argv and asserts the code is absent from it.
- `login --ip` with no `--serial` takes the serial from the certificate CN; a `--serial` that differs from the CN is a FAIL with a masked serial in the text and no profile written.
- A refused CONNACK writes no profile and no Keychain item, exits 3, and the message names the code source and the mode-specific next step from section 5.4.
- With no `printers.json`, `doctor`, `check`, `build`, `sweep` and `printer status` produce the 0.4.0 output byte for byte (the existing tests pass unchanged).
- `--ip` beats the profile IP; `--serial` beats the profile serial; `--no-printer` skips the read; `--printer NAME` with an unknown name is usage, exit 2.
- The profile IP that fails to connect triggers the probe; a certificate-serial match updates `ip` and prints one line; a mismatch stores nothing.
- `printer list --json` is one document with no code; `printer list` prints the IP and the masked serial and nothing else identifying.
- `forget` removes the profile and the Keychain item, and `--keep-code` leaves the item.
- `probe` stores nothing and reports mode `unknown` when no NOTIFY arrives within 3 s.
- `__version__` and `.claude-plugin/plugin.json` move to 0.5.0 together; every `--expect-version`, `shambu@v` and `must print: shambu` literal in both skills and the README follows (`tests/test_plugin_manifest.py:31-49`); CHANGELOG has an `[Unreleased]` entry with Added (profiles, `login`, `list`, `forget`, `probe`, `--printer`, the /24 probe), Changed (error text, doctor lines) and Security (code in the Keychain, never on argv).
- `rg -n '192\.168|access_code|/Users/'` over the diff finds only `access_code` as a config key name.

## 8. Open questions

1. **When does the X2D regenerate the code?** One X2D observation says a LAN-only toggle does. A 2023 P1P thread agrees; SimplyPrint says the toggle does not rotate it; a firmware update and the refresh icon do. Test: note the code, toggle LAN-only off and on, compare. About 10 minutes per toggle including the Wi-Fi reconnect. The section 5.4 text lists every cause until this is settled.
2. **Does the X2D send NOTIFY in each mode on current firmware?** `SsdpListen` heard nothing in 145 s on 2026-10-16 (cloud) and nothing on 2026-10-27 (LAN-only). Test: `shambu printer discover --ssdp 1900` in each mode while Studio is closed, on the same Wi-Fi as the printer. If nothing arrives in either mode, `mode` stays `unknown` unless `login` is told, and a `--mode lan|cloud` flag on `login` is the fallback.
3. **The touchscreen path to the code on the X2D.** Third parties disagree (Settings > General > LAN-Only mode, or Settings > LAN Mode). The official page returned HTTP 402 to the fetcher. Read it off the printer once and fix the section 5.4 text.
4. **A later cloud refresh.** One `GET /v1/iot-service/api/user/bind` call returns `dev_access_code` for every bound printer, which is how ha-bambulab heals a rotated code in cloud mode. It needs an account token in the Keychain (about 90 days), a login flow with e-mail code or TOTP, and a Chrome TLS fingerprint to pass Cloudflare. Decide after 0.5.0 whether the SECURITY.md promise "no network calls other than the printer" is worth changing for it.
5. **The SEARCH port.** Studio sends SSDP SEARCH requests, and the printer appears not to answer them. If an active SEARCH did work, discovery across a sleeping ARP table would be instant. A packet capture while Studio runs settles it. Not needed for 0.5.0: the /24 probe covers it.

## 9. Files touched

- `src/shambu/profiles.py` (new): schema, `validate_profile`, the mode and source enums, no I/O.
- `src/shambu/vault.py` (new): `get(serial)`, `put(serial, code)`, `delete(serial)` over `/usr/bin/security -i` with an injectable runner; `UnavailableVault` on other platforms.
- `src/shambu/conf.py`: `PRINTERS` path, `load_profiles`, `save_profiles`, `update_profiles`, `select_profile(name)`, `sensitive_values` extended, the reworded `access_code` error.
- `src/shambu/printer.py`: `CredentialSource` protocol with `ProfileCredentials`, `StudioCredentials`, `OneShotCredentials`; `SubnetProbe` beside `ArpProbe`; `certificate_serial(ip)`; the reworded errors; `outward_secrets` extended; the `code` watch category.
- `src/shambu/facade.py`: `printer_profile` field, `Bambu.printer()` applies it, doctor's printer section reports source and mode.
- `src/shambu/cli.py`: `--printer`, `--access-code-stdin`, `--access-code-file`, `--from-studio`, `--name`, `--default`, `--keep-code`; `printer login|list|forget|probe`; `redact_ip` on the `main` exit path (`cli.py:1440-1446`, which prints an unredacted IP today).
- `tests/`: `test_profiles.py`, `test_vault.py`, extensions to `test_printer_conf.py`, `test_printer_tls.py`, `test_cli.py`, `conftest.py`.
- `skills/shambu/SKILL.md` lines 20, 24-26, 129, 133, 142 and 190; `skills/setup/SKILL.md` lines 51-56, 70-75 and 90 (which names `PrinterError` where the code raises `ConfError`); a new setup step 4 that runs `shambu printer login`.
- `skills/shambu/references/04-app-printer-control.md`: a section for the profile store, the Keychain, the mode facts of section 3 above, and the Studio config facts (`user_access_code`, `user_access_dev_ip`, the 5 s save, the clear on refusal).
- `SECURITY.md:11-13`, `DISCLAIMER.md:19-20`, `REVIEW-RUBRIC.md` items 2 and 14, `CONTRIBUTING.md` "Where things live", `README.md:45,153`, `CHANGELOG.md`.

## 10. Evidence

- shambu read path: `printer.py:143-341` (discovery), `:411-431` (TLS context), `:509-574` (`MqttReader.read`), `:1115` (default discovery), `:1182-1216` (`resolve`), `:1320-1341` (watch categories), `:1365-1420` (secrets and `public_raw`); `conf.py:33-45`, `:48-73`, `:108-286`; `facade.py:97-100`, `:161-179`, `:318-329`; `cli.py:135-138`, `:216-276`, `:1159-1212`, `:1440-1446`.
- Studio: `GUI_App.cpp:2999-3000,3716-3721` (config save cadence), `:2280-2290` (clear on refusal), `:2275-2276`; `DeviceManager.cpp:466-535` (`user_access_code`, `user_access_dev_ip`), `:1267,2632` (`get_access_code` on connect); `DevManager.cpp:126-143` (SSDP fields), `:292`, `:883-887`; `ReleaseNote.cpp:2050-2080` (the manual IP dialog refuses a cloud-mode printer).
- ha-bambulab: `pybambu/bambu_client.py` on_disconnect and `coordinator.py` `_async_handle_access_denied` (CONNACK 5 as a rotated code), `config_flow.py:340-400` (cloud MQTT by default, `local_mqtt` option), `pybambu/const.py:282-285` (the signature bit); issues #1863, #1973, #1999, #2036.
- Bambu: "Firmware Update Introducing New Authorization Control System" (16 Jan 2025, updated 17 and 20 Jan): status pushes unaffected. Developer Mode wiki (fetched as snippets only, HTTP 402).
- Community: Doridian/OpenBambuAPI `mqtt.md`, `cloud-http.md`; the SSDP NOTIFY capture gist (Benny-Git); jonans/bsnotify (ports 2021 and 1990, SEARCH unanswered); OctoEverywhere and SimplyPrint help pages on the access code; `man security` (tested here: `-i` keeps the secret off argv, exit 44 for a missing item, no prompt when `security` is both creator and reader).
- The 2026-10-27 session: doctor, discover, the subnet probe, the refused login, the hand edit, and the successful read at 10:57 UTC.

## 11. Decisions after review (2026-10-27)

- **The code lives in a 0600 file, not the Keychain.** The Keychain write failed from a non-GUI session ("User canceled the operation"), so the code lives in `credentials.json` (mode 0600) under the XDG config dir (`$SHAMBU_CONFIG_DIR`, else `$XDG_CONFIG_HOME/shambu`, else `~/.config/shambu`), beside `printers.json`. Sections 4.1, 4.2, 4.5, 5.1, 5.2, 6 and 7 are superseded where they name the Keychain, `security` or Application Support for the profile store.
- **Relocation deferred.** A profile IP that no longer answers is one error line that names the explicit fix, `shambu printer login --printer NAME --ip <new ip>`, and `shambu printer discover`. The profile is written only by `login`.
- **`printer probe` deferred.** `login` runs the same read, and `doctor --printer NAME` reports the profile and the read.
- **SSDP mode detection replaced by `--mode`.** `login --mode lan|cloud` records what the touchscreen shows; with neither, the mode is `unknown`. `printer discover --ssdp N` stays.
- **Schema cut.** `code_source`, `model` and `revision` are dropped. A profile is `name, serial, ip, mode, insecure, added, verified`, and `verified` is the time of the last successful `login`.
