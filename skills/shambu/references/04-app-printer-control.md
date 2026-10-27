# 04 - Controlling Bambu Studio and reading the X2D

> **Provenance and licence notice.** Bambu Studio is free software under the AGPL-3.0
> (https://github.com/bambulab/BambuStudio). The findings in this document come from reading that source at tag
> `v02.08.02.61`, inspecting the shipped macOS binary (symbols, strings, crash reports, a debugger session on a local
> re-signed copy) and running experiments with its command line. Short excerpts are quoted for interoperability only.
> No Bambu Studio code is included in shambu, and shambu is not affiliated with or endorsed by Bambu Lab.
> See `DISCLAIMER.md` at the repository root.

Scope: Bambu Studio 02.08.02.61 (universal binary, team T3UBR9Y3B2) on macOS 26 / Apple Silicon, printer Bambu Lab X2D (model id `N6`, serial prefix `20P`).
Workspace: `scratchpad/ctl-re` ($S). All tests were read-only. No MQTT message was published, no FTP login was made, nothing in the GUI was clicked, and no access code or token was printed or stored.

Source references below use a sparse clone of `bambulab/BambuStudio` master at `47a13ee` (2026-10-08), which is newer than the installed 02.08.02.61 (`926a719`). Line numbers are from master.

## Contents

| Section | What it holds |
|---|---|
| [TL;DR](#tldr) | Short answers to each control and printer question |
| [Track 1 - GUI automation through Accessibility](#track-1---gui-automation-through-accessibility) | What AppleScript and Accessibility can see and click in Studio |
| [Track 2 - Launch and open behaviour](#track-2---launch-and-open-behaviour) | Bundle identity, URL scheme and how Studio opens a file |
| [Track 3 - Printer protocols (read-only use)](#track-3---printer-protocols-read-only-use) | MQTT, FTPS and SSDP details for reading the printer |
| [Track 4 - Studio config files](#track-4---studio-config-files) | Studio config files, where the serial and access code live |
| [Track 5 - Community landscape](#track-5---community-landscape) | Other Bambu tools and libraries, and what to copy from them |
| [Track 6 - Language](#track-6---language) | Why the tool uses Python with uv, and the toolchains present |
| [Recommended command set for the wrapper](#recommended-command-set-for-the-wrapper) | The proposed read-only subcommands |
| [Citations](#citations) | Source links for the claims above |
| [Artefacts in $S](#artefacts-in-s) | Files the investigation left in the scratch workspace |
| [Track 7 - Time and job fields, and the watch log](#track-7---time-and-job-fields-and-the-watch-log-2026-10-21) | The report's time, speed and stage keys, and `printer watch --log` with `--gcode3mf` for `shambu calibrate` |
| [Track 8 - Printer profiles and the credentials file](#track-8---printer-profiles-and-the-credentials-file-2026-10-27) | The config dir, `printers.json` and `credentials.json`, both modes on one transport, what Studio keeps, `printer login` step by step, the trust boundary |

## TL;DR

| Question | Answer |
|---|---|
| Can AppleScript see Studio? | Yes. Accessibility is granted to the terminal. The menu bar is fully readable with shortcuts and enabled state. The window tree shows only the sidebar (unnamed wx buttons, preset text fields). The top toolbar (Prepare / Preview / Device, **Slice plate**, **Print plate**) is custom-drawn and not exposed. |
| Menu path to slice? | None. "Slice plate" exists only as the toolbar button and the key `⌘R`. |
| Menu path to export? | Yes. `File > Export > Export plate sliced file...` (`⌘G`) and `Export all plate sliced file...`. |
| Dangerous shortcut | `⌘⇧G` = **Print plate**. One modifier away from `⌘G` (export). An automation must never send `G` keystrokes; it must click the menu item through AX instead. |
| URL scheme | Only `bambustudioopen://<url-encoded http(s) URL>` (model download, trusted-host check, otherwise a Yes/No dialog). No `bambustudio://` scheme is registered on macOS (the `bambustudio://open?file=` form is a command-line argument path only). No local-file URL, no slice action. |
| `open -a BambuStudio x.3mf` while running | With `app.single_instance = "False"` (this Mac), the running instance spawns a **second BambuStudio process** with the file (`start_new_slicer`). With `single_instance = "True"`, the running window calls `plater()->load_files()` (project-replace dialogs). |
| GUI "slice on open" switch | None. The binary starts the GUI only when the command line has no action (`start_gui = m_actions.empty()`). `--load-settings` is honoured in GUI mode (`mainframe->load_config_file`), but nothing triggers a slice. |
| `--pipe` on macOS | Parsed but a **no-op**: the callback manager is compiled only `#if defined(__linux__)`. Use `result.json`, exit code and `--debug` logs on macOS. |
| Printer read path | MQTT/TLS 8883, user `bblp`, LAN access code, SNI = serial, CA = Studio's bundled `printer.cer`. Subscribe `device/<serial>/report`, publish one `pushall` to `device/<serial>/request`. Status pushes are explicitly **not** covered by Authorization Control. |
| Was the X2D reachable now? | Not tested. No SSDP NOTIFY arrived in 145 s of passive listening, Studio held no LAN sockets, and no printer IP is stored in the readable config. |
| Language | **Python, single-file `uv run --script`** with `paho-mqtt` as the only dependency. Rationale in track 6. |

---

## Track 1 - GUI automation through Accessibility

### Commands run and results

```sh
osascript -e 'tell application "System Events" to tell process "BambuStudio" to get name of every menu bar item of menu bar 1'
# -> Apple, Bambu Studio, File, Edit, View, Window, Help

osascript -e 'tell application "System Events" to tell process "BambuStudio" to get name of windows'
# -> * plate-10-root-coupons-D6

osascript $S/menus.applescript      # walks every menu item, reads AXMenuItemCmdChar / AXMenuItemCmdModifiers / enabled
# full output: $S/menus.txt

osascript -e 'tell application "System Events" to tell process "BambuStudio" to get entire contents of window 1' > $S/ax-entire.txt
# 8.5 KB; element counts: 33 button, 19 text field, 18 static text, 18 checkbox, 3 image, 3 group, 1 scroll bar

osascript $S/btns.applescript       # title/description/help/position of each button, value of each text field
```

No permission error. Accessibility is granted to the terminal host (no `-1719` / "not allowed assistive access" error).

### What the menus expose (AX `mods`: 0 = ⌘, 1 = ⇧⌘, 2 = ⌥⌘, 8 = no ⌘)

| Menu | Item | Shortcut |
|---|---|---|
| File | New Project | ⌘N |
| File | Open Project... | ⌘O |
| File | Recent projects > 1..4 (full paths) | - |
| File | Save Project / Save Project as... | ⌘S / ⇧⌘S |
| File > Import | Import 3MF/STL/STEP/SVG/OBJ/AMF... | ⌘I |
| File > Import | Import Configs... | - |
| File > Export | Export all objects as one STL / as STLs / Export Generic 3MF | - |
| File > Export | **Export plate sliced file...** | ⌘G |
| File > Export | Export all plate sliced file... / Export G-code... / Export toolpaths as OBJ / Export Preset Bundle | - |
| File | Publish to MakerWorld, Batch Preset Management | - |
| Edit | Undo ⌘Z, Delete all ⇧⌘D, Clone ⌘K, Select all ⌘A | |
| View | Default/Top/Bottom/Front/Rear/Left/Right/Iso ⌘0..⌘7, Show Overhang ⌘L, labels ⌘E / ⇧⌘E | |
| Bambu Studio | Settings… ⌘, | |
| Help | Keyboard Shortcuts, Show Configuration Folder, Open Network Test | |

There is **no Slice, no Print, no Send** item in any menu.

Shortcuts that exist only as key handlers (`MainFrame.cpp` char hook, lines ~715-740, and `KBShortcutsDialog.cpp` 182-186):

| Keys | Action | Guard in code |
|---|---|---|
| ⌘R | Slice plate (`EVT_GLTOOLBAR_SLICE_PLATE`) | `check_slice_version_policy() && m_slice_enable` |
| ⌘G | Export plate sliced file | `can_export_gcode()` |
| **⇧⌘G** | **Print plate** (`EVT_GLTOOLBAR_PRINT_PLATE`, opens the send-to-printer dialog) | `get_enable_print_status()` |
| ⌘J | Print host upload queue | - |

### What the window tree exposes

- The sidebar only: printer preset row, "Main Nozzle" / "Auxiliary Nozzle" groups (Diameter, Flow, AMS "Not installed"), "Project Filaments", "Process" with 19 text fields (layer height 0.2, line widths 0.42/0.45/0.5, etc.) and 18 checkboxes. Buttons have empty titles; only some carry `help` text ("Click to edit preset", "Synchronize filament list from AMS", "Save current Process", "Compare presets", "Search in preset").
- The top bar (Prepare / Preview / Device tabs, Slice plate, Print plate) and the 3D canvas are custom-drawn on a GL canvas and **do not appear** in the AX tree. An agent cannot read slicing progress, estimated time or "sliced" state from AX except through the enabled state of `File > Export > Export plate sliced file...`.

### Feasibility

| Action | Feasible | How | Risk |
|---|---|---|---|
| Read state (window title = project name and `*` when dirty, recent projects, preset field values) | Yes | AX read | None |
| Open a project | Yes | `File > Open Project...` via AX `click menu item`, then drive the NSOpenPanel (`⇧⌘G` inside the **panel** = "Go to folder") | Medium: `⇧⌘G` outside the panel is Print plate |
| Slice plate | Only by keystroke ⌘R | `keystroke "r" using command down` with Studio frontmost | Medium: needs focus; a wrong modifier or a focused text field changes the result |
| Know that slicing finished | Weak | Poll `enabled` of "Export plate sliced file..." | It is enabled after any slice, not only the newest one |
| Export sliced file | Yes | AX click on `Export plate sliced file...`, then drive NSSavePanel | Low if done by menu click |
| Print / send | Must never be automated | - | - |

Verdict: GUI automation works but is brittle. Use it only for "open this project in Studio for the user". The headless CLI produces the same `.gcode.3mf` that ⌘R + ⌘G produce, without focus or dialog risk.

### Sketch of a safe flow with an user gate

1. `studio state` (read-only): AX read of window title, `frontmost`, and whether a modal sheet is present (`sheets of window 1`). Refuse if a sheet or dialog is open.
2. User gate: print the exact actions ("open X.3mf in a new Studio window; slice plate 1; export to Y.gcode.3mf") and require an interactive `y` on a TTY or a `--i-am-the-user` flag that the skill never sets on its own.
3. Open with `open -a BambuStudio X.3mf`. With `single_instance=False`, this starts a separate process and leaves the existing window untouched. Wait until a new window whose title equals the project name appears.
4. Bring only that window to front (`AXRaise` + `set frontmost`), re-check the title, then send ⌘R. Never send any key that contains `g`.
5. Poll for the "Export plate sliced file..." enabled state, then click the menu item by name, and fill the save panel by AX (text field value + `AXConfirm` on "Save").
6. Verify the output zip (`Metadata/plate_1.gcode` present, md5 sidecar) and stop. Do not close or quit any window that the tool did not open.

Preference: skip steps 4-5 and slice with the CLI; use the GUI only for step 3 so the user reviews in Studio and presses Print themselves.

---

## Track 2 - Launch and open behaviour

### Info.plist (`plutil -p /Applications/BambuStudio.app/Contents/Info.plist`)

- `CFBundleIdentifier` `com.bambulab.bambu-studio`, version `02.08.02.61`.
- Document types (role Viewer, rank Alternate): `stl`, `obj`, `amf`, `3mf`, `gcode`.
- `CFBundleURLTypes`: one scheme only, **`bambustudioopen`**. `bambustudio://` is not registered on macOS.
- `NSAllowsArbitraryLoads = true`.

### URL formats (GUI_App.cpp)

- `bambustudioopen://<url-encoded https URL>` → `MacOpenURL` → `extract_model_download_url_from_mac` strips the prefix and URL-decodes → `is_trusted_model_download_url()` (release builds) else a "This file is not from a trusted site" Yes/No dialog → `request_model_download(url)` (downloads, then opens). Only `http://` / `https://` targets are accepted; there is no `file://` path.
- `bambustudio://open?file=<url>` is recognised only as the **first command-line argument** at start-up (`post_init`, `extract_model_download_url_from_open`), not as a registered scheme on macOS.
- Neither form can slice, print, or select presets. Neither is useful for local projects. Use plain file opens.

### `open -a BambuStudio file.3mf` when the app already runs

From `GUI_App::MacOpenFiles` (master line ~7852) and `Utils/Process.cpp`:

- `app.single_instance` in BambuStudio.conf is `"False"` on this Mac.
- `single_instance == false` and the file list holds a `.3mf` → `start_new_slicer(names)` → `boost::process::spawn(<bundle>/Contents/MacOS/BambuStudio, [files])`. Result: a **second, independent Studio process and window**. The current project is not touched, so no "save changes?" prompt in the running window.
- `single_instance == true` → `plater()->load_files()` in the running window, which shows the project-load / unsaved-changes dialogs.
- Side effects of the second process: it reads the same config, logs in to the same account and starts its own network plugin. That costs about 1-2 GB RAM and duplicates the printer connection. Treat it as acceptable for "open for review", not for a loop.
- Not tested live, because the running Studio was the user's session.

### Command-line facts relevant to a wrapper

- `--pipe <fifo>`: parsed on all platforms, but `g_cli_callback_mgr.start()` is inside `#if defined(__linux__) || defined(__LINUX__)` (`src/BambuStudio.cpp`, around line 1799). On macOS it logs "Will use pipe" and does nothing else. `strings` on the installed binary shows the "Will use pipe" string, which matches this. On Linux the FIFO receives one JSON line per update: `{"plate_index","plate_count","plate_percent","total_percent","message"|"warning"}`.
- GUI vs headless: `start_gui = m_actions.empty() && !downward_check`. Any action flag (`--slice`, `--export-3mf`, ...) makes the run headless. There is no flag that opens the GUI and then slices.
- In GUI mode, `--load-settings` (last entry) and extra `--key value` options are applied to the GUI (`mainframe->load_config_file` / `load_config`), so `BambuStudio --load-settings machine.json;process.json file.3mf` can preload presets for the user.

---

## Track 3 - Printer protocols (read-only use)

### Transport and trust

| Item | Value | Source |
|---|---|---|
| MQTT | `mqtts://<ip>:8883`, user `bblp`, password = LAN access code | OpenBambuAPI mqtt.md |
| TLS verify | CA bundle = `/Applications/BambuStudio.app/Contents/Resources/cert/printer.cer` (5 certs: `BBL CA2 RSA` x2, `BBL CA2 ECC` x2, `BBL CA` to 2032). SNI / hostname = **printer serial** (cert CN). Python 3.13+ needs `ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT`. | openssl on the bundle; OpenBambuAPI tls.md |
| Topics | sub `device/<serial>/report`; pub `device/<serial>/request` | mqtt.md |
| Full report | `{"pushing":{"sequence_id":"0","command":"pushall","version":1,"push_target":1}}` | mqtt.md |
| FTPS | `ftps://<ip>:990`, implicit TLS, user `bblp`, LAN access code; `LIST`/`NLST`/`RETR` are read-only (`ftplib.FTP_TLS` needs an implicit-TLS subclass and `prot_p()`) | OpenBambuAPI ftp.md |
| File tunnel | TCP 6000 framed JSON on some models (list/upload/download/delete) | lan-file-tunnel.md |
| Camera (X1/P2S/H2/X2D) | `rtsps://bblp:<code>@<ip>:322/streaming/live/1`; off by default on H2-era firmware. Enable on the printer: Settings > General > "LAN Mode Liveview". `print.ipcam.rtsp_url` reports the URL or `"disable"`. | OpenBambuAPI video.md; pybambu models.py |
| Camera (A1/P1) | JPEG stream on TLS 6000 | video.md |
| X2D in Studio's own table | `printers/N6.json`: `ipcam.liveview = {"remote":"tutk"}`, `file = {"local":"local","remote":"tutk"}`, `support_ssl_for_mqtt`, `support_tunnel_mqtt`, `support_mqtt_alive`, `support_send_to_sd`, `support_cloud_print_only: false` | local file |
| Discovery | Printer sends SSDP-style `NOTIFY` (UDP, unicast/broadcast to 2021; multicast 239.255.255.250:1990) with `Location` (IP), `USN` (serial), `DevModel.bambu.com`, `DevName`, `DevConnect` (lan/cloud), `DevBind`, `Devseclink`, `DevVersion`. | Studio DeviceManager / community |

### `pushall` fields that matter for an X2D (dual nozzle)

Classic single-nozzle keys in `print` (still present): `gcode_state` (IDLE/PREPARE/RUNNING/PAUSE/FINISH/FAILED), `mc_percent`, `mc_remaining_time` (min), `layer_num`, `total_layer_num`, `subtask_name`, `gcode_file`, `task_id`, `subtask_id`, `print_type`, `stg_cur`, `mc_print_stage`, `mc_print_sub_stage`, `print_error`, `mc_print_error_code`, `fail_reason`, `nozzle_temper`, `nozzle_target_temper`, `bed_temper`, `bed_target_temper`, `chamber_temper`, `nozzle_diameter`, `nozzle_type`, `spd_lvl`, `spd_mag`, `lights_report`, `home_flag`, `hw_switch_state`, `wifi_signal`, `sdcard`, `ipcam`, `xcam`, `upgrade_state`, `upload`, `hms`, `ams`, `vt_tray`, `online`, `net`, `cfg`, `fun`, `stat`, `aux`.

Dual-nozzle block `print.device` (Studio `DevExtruderSystem.cpp` / `DevNozzleSystem.cpp`, pybambu `models.py`):

- `device.extruder.state` (bit-packed): bits 0-3 extruder count, 4-7 current extruder id, 8-11 target extruder id, 12-14 switch state, 15-18 loading extruder id, 19 busy-loading.
- `device.extruder.info[]`, one per extruder, `id` 0 or 1:
  - `temp`: low 16 bits current °C, high 16 bits target °C.
  - `info`: bit 1 filament at extruder, bit 2 filament in buffer, bit 3 nozzle present.
  - `snow` / `spre` / `star`: current / previous / target slot. Low 8 bits slot, high 8 bits AMS id (`0xFFFF` = none).
  - `stat`: low 16 AMS state / filament-change step, high 16 RFID state.
  - `hnow`: nozzle id currently mounted. `filam_bak`: backup slots.
- X2D naming from `printers/N6.json` `tool_head_display_names`: **id 0 = Auxiliary (right)**, **id 1 = Main (left)**. The cached HMS text agrees ("right (Aux) extruder").
- `device.nozzle.info[]`: `id`, `diameter`, `type` (e.g. hardened steel / high-flow code), `fila_id`, `wear`.
- `device.bed.info.temp`, `device.ctc.info.temp` (chamber): the same packed low/high words.
- Top-level `nozzle_temper` still mirrors the main extruder only. Do not use it for the X2D.

AMS: `print.ams.ams[]` → `id`, `humidity`, `temp`, `tray[]` (`id`, `tray_type`, `tray_sub_brands`, `tray_color` RRGGBBAA, `tray_info_idx` = filament_id such as `GFG99`, `nozzle_temp_min/max`, `remain`, `k`, `n`, `tag_uid`, `tray_uuid`). `ams.tray_now` / `tray_tar` / `tray_pre`, `ams_exist_bits`, `tray_exist_bits`. External spools: `vt_tray` (and on dual-nozzle firmware `vir_slot[]`, one per extruder, ids 254/255).

### HMS codes

`print.hms[]` = `{"attr": u32, "code": u32}`. Studio (`DevHMS.cpp`) decodes:

```
module = attr>>24 & 0xFF ; module_num = attr>>16 & 0xFF ; part = attr>>8 & 0xFF ; reserved = attr & 0xFF
level  = code>>16 (1 fatal, 2 serious, 3 common, 4 info) ; msg = code & 0xFFFF
long code = "%02X%02X%02X%02X00%02X%04X"  -> e.g. 0700_8B00_0002_0006 (wiki form inserts underscores)
```

Text lookup works offline: `~/Library/Application Support/BambuStudio/hms/hms_en_20P.json` (X2D serial prefix `20P`) holds `data.device_hms.en[]` (2518 entries, `ecode` = 16-hex long code, `intro`) and `data.device_error.en[]` (815 entries, `ecode` = 8-hex `print_error`). `hms_action_20P.json` holds the actions. Version `202609231145`.

### Authorization Control and the X2D

- Bambu's own post (16 Jan 2025, updated 17 and 20 Jan) lists what needs authorization: binding, remote video, firmware upgrade, **starting a print (LAN or cloud)**, motion / temperature / fan / AMS / calibration control. It lists as **unaffected**: "Sending status information from the printer (e.g., MQTT status push for tools like HomeAssistant)", SD-card print start, and general operations.
- Developer Mode (LAN-only mode only) turns Authorization Control off and opens MQTT, liveview and FTP for third parties. It disconnects the printer from Bambu Cloud and Handy, and Bambu gives no support for it.
- Third-party X2D guides (SimplyPrint) say current X2D firmware needs **LAN-only + Developer Mode** for control, and that cloud mode is monitor-only. The `beambam` package states that the X2D/H2D family requires RSA-SHA256-signed MQTT commands at all times.
- Consequence for this tool: read-only status (subscribe + `pushall`) should work in LAN mode without Developer Mode, because status pushes are exempt. Whether the X2D's **local** broker accepts a `bblp` login while the printer is in **cloud** mode is not confirmed for current firmware. Verify with the `probe` subcommand (connect + subscribe + one `pushall`) before building on it. If the broker refuses, the user must enable LAN-only mode (and, for liveview, "LAN Mode Liveview"). The tool needs no Developer Mode, because it never sends commands.
- `pushall` itself is in the `pushing` namespace, not a control command. ha-bambulab still sends it after the authorization firmware. Note that pybambu also publishes `{"info":{"command":"get_version"}}` on connect. Our tool must not, under the "pushall only" rule.

### Live checks on this Mac

```sh
lsof -nP -iUDP:2021 ; lsof -nP -iTCP:8883 ; netstat -an | grep -E '\.(2021|1990|8883|990|322|6000) '
# -> nothing. The Studio process (PID <n>) held no LAN sockets at all.

python3 -I $S/tools/ssdp_listen.py 25     # bind 2021 + 1990 with SO_REUSEADDR/REUSEPORT, join 239.255.255.250, listen only
python3 -I $S/tools/ssdp_listen.py 120
# -> "bound 2021", "bound 1990", "unique senders: 0" (both runs)
```

Interpretation: the X2D did not announce itself on this LAN during the 145 s window. Possible causes: printer off or asleep, a different subnet / VLAN, or AP client isolation. Studio was not holding UDP 2021 at the time, so the port was free (when Studio runs with LAN discovery it binds 2021, and a second listener needs `SO_REUSEPORT`). No MQTT or FTPS connection was tried, because no printer IP was known and none is stored in readable config. A port scan of the LAN was not done.

Incident to note: the Studio GUI process (PID <n>, opened at 10:49) **exited at about 11:23** during this session. No crash report exists for that PID. The `.ips` reports at 11:23-11:25 belong to other short-lived BambuStudio CLI runs (PID <n>, crash in `FilamentGroup::calc_one_change_budget`) started by the parallel `bin-re` agent. This track only did AX reads and a passive UDP listen. The cause of the GUI exit is unknown. A print in progress runs on the printer and does not depend on Studio.

---

## Track 4 - Studio config files

Path: `~/Library/Application Support/BambuStudio/`.

| File / dir | Format | Notes |
|---|---|---|
| `BambuStudio.conf` | Plain JSON (no MD5 trailer in this version) | Main app config |
| `BambuNetworkEngine.conf` | Binary / encrypted (not UTF-8) | Network plugin state. Do not parse. |
| `log/*.log.enc`, `debug_network_*.log.enc` | Encrypted | Not useful |
| `printers/<model_id>.json` | JSON | Per-model capability table (`N6` = X2D) |
| `hms/hms_<lang>_<prefix>.json` | JSON | Offline HMS text |
| `system/BBL.json`, `system/BBL/{machine,process,filament}/*.json` | JSON | System presets, X2D files: `Bambu Lab X2D.json`, `Bambu Lab X2D 0.2/0.4/0.6/0.8 nozzle.json`, template G-code files |
| `user/<uid>/{machine,process,filament}/` and `user/default/...` | JSON + `.info` | User presets. **All empty on this Mac** (0 JSON files). |
| `cert` (in the app bundle) | PEM | `printer.cer` CA bundle |

### `BambuStudio.conf` structure (keys only; secrets shown as type/length)

```
access_code: { "<serial>": <str len=8> }          # LAN access code per printer. SECRET. Read in memory only.
app: { ... 117 keys ... }
  user_last_selected_machine: "<serial>"          # the printer selected in the Device tab
  preset_folder: "<uid>"                          # -> user/<uid>/
  curr_bed_type: "4"                              # index into BedType enum, see below
  single_instance: "False"
  enable_ssl_for_mqtt: "True", enable_ssl_for_ftp: "True"
  developer_mode: "False", internal_developer_mode: "False"
  region: "<region>", language: "<lang>", sync_user_preset: "True", slicer_uuid: <str len=36>
  last_backup_path, window_mainframe, ...
presets: {
  machine: "Bambu Lab X2D 0.4 nozzle",
  process: "0.20mm Standard @BBL X2D",
  filaments: ["Bambu PETG Basic @BBL X2D 0.4 nozzle"],
  filament_colors: "#00AE42", flush_volumes_matrix, flush_volumes_vector, filament_* mixing keys
}
models: [ { vendor: "BBL", model: "Bambu Lab X2D", nozzle_diameter: "0.2;0.4;0.6;0.8" } ]
nozzle_volume_types: { "Bambu Lab X2D 0.4 nozzle": "Standard,Standard" }
N6: { bed_leveling, flow_cali, nozzle_offset_cali, timelapse }    # last print options for model N6
print: { bed_leveling, flow_cali, timelapse }
filaments: [ 273 visible filament preset names ]
recent: { last_opened_folder, settings_folder }
recent_projects: { "001".."004": <paths> }
firstguide, liveview{auto_stop_liveview}, header
```

- Printer IP is **not** stored in `BambuStudio.conf`. It is probably in the encrypted network-engine state. The wrapper must get the IP from SSDP or from its own config.
- `curr_bed_type` maps to `BedType` (`PrintConfig.hpp` / `.cpp`): 0 Default, 1 Cool Plate, 2 Engineering Plate, 3 High Temp Plate (Smooth PEI), 4 **Textured PEI Plate**, 5 Supertack Plate. The 3MF / CLI value is the string (`curr_bed_type = Textured PEI Plate`).
- Safe reader rule for the library: load JSON, never log `access_code`, pass the code to the MQTT/FTP client in memory, and redact any key that matches `access|code|token|ticket|pass`.

### User preset format (from `Preset.cpp`, since none exist locally)

A user preset is `user/<uid>/<kind>/<name>.json` plus `<name>.info`:

```json
{ "name": "My PETG fast", "inherits": "Bambu PETG Basic @BBL X2D 0.4 nozzle", "from": "User",
  "is_custom_defined": "0", "version": "2.8.x.x",
  "filament_settings_id": ["My PETG fast"],   // print_settings_id / printer_settings_id for the other kinds
  "<only the overridden keys>": ["..."] }
```

`.info` is `key = value` lines: `sync_info`, `user_id`, `setting_id`, `base_id`, `updated_time`. A loader must resolve `inherits` recursively through `user/<uid>` then `system/BBL` (system files chain to `fdm_*_common.json` the same way) and then overlay the user keys. Vector values stay per-extruder arrays (X2D = 2 entries for machine/extruder keys). With `sync_user_preset=True`, cloud presets appear here after Studio syncs them.

---

## Track 5 - Community landscape

| Project | Lang / licence | What it does | Presets / inheritance | Headless | Worth copying |
|---|---|---|---|---|---|
| [ha-bambulab / pybambu](https://github.com/greghesp/ha-bambulab) | Python, MIT | Full MQTT model of every printer incl. H2D/X2D (`dual_nozzle_printers`), FTPS, RTSP, HMS | n/a | n/a | Field decoding (packed temps, `snow` slots, `rtsp_url`), TLS context with Bambu CA, reconnect logic |
| [Doridian/OpenBambuAPI](https://github.com/Doridian/OpenBambuAPI) | Docs | MQTT, FTP, video, TLS/SNI, LAN file tunnel, cloud X.509 | n/a | n/a | Canonical protocol reference; TLS pitfalls (SNI = serial, Py 3.13 strict flag) |
| [BambuTools/bambulabs_api](https://github.com/BambuTools/bambulabs_api) | Python, MIT | MQTT + FTP client with control helpers | No | n/a | Little: H2D untested, X1 camera incomplete |
| [DMontgomery40/bambu-printer-mcp](https://github.com/DMontgomery40/bambu-printer-mcp) | TypeScript (Node 24) + C++ helper, GPL-2.0 | MCP server: slice via Studio/Orca CLI, MQTT status, FTPS (basic-ftp), RTSP via ffmpeg, X2D printing through the official networking plug-in (macOS helper) | "require the exact machine preset and resolve BBL dependencies before slicing" | CLI | Human preflight gate for print/heat; check for embedded `Metadata/plate_<n>.gcode` before deciding to slice; AMS auto-match on `tray_info_idx` + colour. GPL: copy ideas, not code. |
| [tobiasbischoff/bambu-cli](https://github.com/tobiasbischoff/bambu-cli) | Go | `status`, `watch`, `print start`, camera snapshot (port 6000), profiles in `~/.config/bambu/config.json` | No | n/a | `--access-code-file` / `--access-code-stdin` (never a flag), confirm on destructive actions |
| [mattcar15/bambu-connect](https://github.com/mattcar15/bambu-connect) | Python (PyPI) | MQTT status, camera, FTP list, send print | No | n/a | Small and readable; old (2023-24) |
| bambu-js (AndrewLemons) | TypeScript | MQTT + FTP | No | n/a | - |
| [schwarztim/bambu-mcp](https://github.com/schwarztim/bambu-mcp) | TypeScript | MQTT control with X.509 signing from extracted Bambu Connect keys | No | n/a | **Do not copy**: uses an extracted private key |
| hunter-stradley/bambustudio-mcp | Python | subprocess wrapper around the Studio CLI returning output paths, time and filament | Partial | CLI | Result object shape |
| [sksat/bambu-rs](https://github.com/sksat/bambu-rs) | Rust | CLI wrapper (`--slice`, `--load-settings`, `--allow-newer-file`) | No | CLI | - |
| [Printago CLI references](https://printago.io/blog/bambu-studio-cli-reference) | Docs | Flag tables for Studio and Orca | Notes precedence: CLI flags > load-settings > embedded 3MF | Notes temp-file bloat, `--mstpp` timeout | Run each job in its own temp dir; use `--skip-useless-pick` |
| [OrcaSlicer CLI](https://www.orcaslicer.com/wiki/cli/cli_mode) | C++ | Same CLI family | Same JSON shape, **not interchangeable** with Studio profiles | Linux builds may need `xvfb-run` | - |
| linuxserver/orcaslicer, mugenoesis/trident | Docker | GUI in browser (KasmVNC) / headless Orca with web UI | - | Docker | Not needed on this Mac |
| [lib3mf / py-lib3mf](https://pypi.org/project/lib3mf/) | C++ with Python bindings, BSD | Full 3MF core + Production extension read/write | - | - | Overkill: Bambu metadata (`Metadata/*.config`) is outside its model, so stdlib `zipfile` + `xml.etree` is simpler |
| trimesh | Python, MIT | Mesh I/O incl. 3MF export | - | - | Only for geometry checks (bounding box, watertight); not for Bambu projects |

Known CLI pitfall (Bambu forum "CLI cannot apply external presets"): `--uptodate-settings` / `--uptodate-filaments` can be ignored. Projects with embedded presets slice correctly. That matches the approach of writing settings into the 3MF and resolving inheritance in the wrapper.

Ideas to copy: user preflight gate (bambu-printer-mcp), access code from file/stdin/keychain only (bambu-cli), field decoders and CA handling (pybambu), embedded-G-code detection, per-job temp dirs and `--mstpp` cap (Printago). Ideas to reject: extracted signing keys, any print/upload path, Docker/xvfb (not needed on macOS, where the CLI runs without a window server dialog for slicing).

---

## Track 6 - Language

Requirements: shell out to the Studio binary, zip + XML + JSON, MQTT over TLS with a custom CA and SNI, FTPS implicit TLS (listing), optional `osascript`, zero install friction on this Mac, colocated inside a Claude skill.

Toolchains present: `uv 0.10.7`, `python3 3.13.1` (uv picked 3.14.3), `node 22.12`, `swift 6.3.3`, `ffmpeg 7.1`. **No Go, no Rust**, no `mosquitto_sub`.

Test: a PEP 723 script with `dependencies = ["paho-mqtt>=2.1"]` ran with `uv run --quiet --script uvprobe.py` in 0.06 s (cached) and imported `paho.mqtt 2.1.0`, `ssl`, `ftplib`, `zipfile`, `xml.etree`.

| Option | Fit | Problems |
|---|---|---|
| **Python + uv single file** | stdlib covers zip, XML, JSON, subprocess, `ssl` (custom CA, SNI, flag tweak), `ftplib.FTP_TLS` (implicit-TLS subclass is about 10 lines); one dependency (`paho-mqtt`); `osascript` through subprocess; pybambu is Python, so its decoders port directly; the agent reads and edits it easily | Python 3.13+ X.509 strict flag (one line); about 50 ms start-up |
| TypeScript / Node | `mqtt` and `basic-ftp` are good; bambu-printer-mcp exists | Needs `node_modules` or a bundler inside the skill; zip needs a dependency; GPL reference code |
| Go | Single static binary, good TLS | Not installed; needs a build step and a checked-in binary per arch |
| Rust | Same as Go, slower to change | Not installed |
| Swift | Installed; native AX API (better than osascript) | No MQTT in stdlib, package build needed; poor for quick agent edits |

Verdict: **Python, one `uv run --script` file per tool (or one package-less module plus a thin script), stdlib first, `paho-mqtt` as the only dependency**. If the AX part grows, keep it in AppleScript files that the Python CLI calls, not in Swift.

---

## Recommended command set for the wrapper

All subcommands are read-only unless marked. Global options: `--printer <name>` (a profile in `printers.json` in shambu's config dir, with the serial, IP and mode). The access code comes from a one-shot flag (`--access-code-file`, `--access-code-stdin`), then the code `shambu printer login` stored in `credentials.json` (0600), then `BambuStudio.conf` `access_code.<serial>`; it is held in memory and never logged (Track 8).

| Subcommand | Does | Needs |
|---|---|---|
| `studio info` | Version, bundle path, running PIDs, `single_instance`, selected machine/process/filament presets, `curr_bed_type` name, preset folder uid | Read `Info.plist`, `BambuStudio.conf` (redacting reader), `pgrep` |
| `studio state` | Window titles (project name, dirty `*`), frontmost, modal sheet present, enabled state of "Export plate sliced file..." | `osascript` + Accessibility permission |
| `studio open <3mf> [--load-settings ...]` (user gate) | Opens the project for review. With `single_instance=False` this starts a separate Studio process. | `open -a BambuStudio` or the binary with args; TTY confirmation |
| `presets list [--kind machine\|process\|filament] [--printer X2D]` | Lists system and user presets by name | `system/BBL/**`, `user/<uid>/**` |
| `presets resolve <name>` | Flattens the `inherits` chain to one JSON (system → user overrides) for `--load-settings` or 3MF embedding | Same dirs; per-extruder vector handling |
| `slice <3mf\|stl...> --plate N --out x.gcode.3mf` | Headless CLI slice in a temp dir with `--mstpp` cap; parses `result.json`; verifies `Metadata/plate_N.gcode` | Studio binary; covered by the `bambu-3mf` skill / track 1-3 reports |
| `gcode3mf inspect <file>` | Plates, time, filament use, bed type, nozzle map, embedded preset names | `zipfile` + XML (`slice_info.config`, `plate_N.json`, `project_settings.config`) |
| `printer discover [--seconds 30]` | Passive SSDP listen on 2021/1990 with `SO_REUSEPORT`; prints IP, model, `DevConnect`, firmware; serial masked | UDP sockets only |
| `printer probe` (deferred) | TLS connect to 8883 (CA = Studio `printer.cer`, SNI = serial), subscribe report, **one** `pushall`, disconnect; reports whether LAN MQTT is open in the current mode. Not built: `printer login` runs the same read and `doctor --printer NAME` reports the profile and the read (Track 8) | `paho-mqtt`, access code |
| `printer status [--watch N]` | Normalised status: state, %, remaining min, layer, both nozzles (Main id 1 / Aux id 0) temp+target, bed, chamber, active extruder, AMS trays, external spools, `rtsp_url`, HMS decoded with offline text | `pushall` once, then passive reports; `hms_en_20P.json` |
| `printer hms` | Current HMS list → long codes + English text + wiki link | Same |
| `printer files [path]` | FTPS `NLST`/`LIST` of the printer storage (read-only) | `ftplib.FTP_TLS` implicit subclass, `prot_p()` |
| `printer snapshot --out x.jpg` | One frame from `rtsps://...:322/streaming/live/1` when `rtsp_url` is not `"disable"` | `ffmpeg -rtsp_transport tcp -frames:v 1` (installed) |

Never implement: print start, pause, stop, resume, gcode send, upload, light/fan/temperature control, any MQTT publish other than `pushall`, keystrokes that contain `G`.

## Citations

- BambuStudio source: https://github.com/bambulab/BambuStudio (`src/slic3r/GUI/GUI_App.cpp`, `MainFrame.cpp`, `KBShortcutsDialog.cpp`, `DeviceCore/DevExtruderSystem.cpp`, `DeviceCore/DevNozzleSystem.cpp`, `DeviceCore/DevHMS.cpp`, `src/BambuStudio.cpp`, `src/slic3r/Utils/Process.cpp`, `src/libslic3r/Preset.cpp`, `PrintConfig.cpp`)
- OpenBambuAPI: https://github.com/Doridian/OpenBambuAPI (mqtt.md, ftp.md, video.md, tls.md, lan-file-tunnel.md, local-printer-api.md)
- Bambu Lab, "Firmware Update Introducing New Authorization Control System": https://blog.bambulab.com/firmware-update-introducing-new-authorization-control-system-2
- Bambu wiki, Developer Mode: https://wiki.bambulab.com/en/knowledge-sharing/enable-developer-mode (returned HTTP 402 to the fetcher; content taken from search snippets)
- Bambu wiki, third-party integration: https://wiki.bambulab.com/en/software/third-party-integration (same)
- Forum, auth system explained: https://forum.bambulab.com/t/explaining-the-auth-system-in-laymans-terms/136629/21
- Forum, CLI ignores external presets: https://forum.bambulab.com/t/cli-cannot-apply-external-presets/197842
- SimplyPrint X2D guide: https://simplyprint.io/setup-guide/bambu-lab/x2d (and localised variants)
- beambam (signed MQTT note): https://pypi.org/project/beambam/
- ha-bambulab / pybambu: https://github.com/greghesp/ha-bambulab
- bambulabs_api: https://github.com/BambuTools/bambulabs_api
- bambu-printer-mcp: https://github.com/DMontgomery40/bambu-printer-mcp
- bambu-cli: https://github.com/tobiasbischoff/bambu-cli
- bambu-connect: https://github.com/mattcar15/bambu-connect
- bambu-mcp: https://github.com/schwarztim/bambu-mcp
- bambu-rs: https://github.com/sksat/bambu-rs
- Printago CLI reference: https://printago.io/blog/bambu-studio-cli-reference
- OrcaSlicer CLI: https://www.orcaslicer.com/wiki/cli/cli_mode
- linuxserver orcaslicer: https://docs.linuxserver.io/images/docker-orcaslicer
- lib3mf: https://pypi.org/project/lib3mf/

## Artefacts in $S

`menus.applescript`, `menus.txt`, `btns.applescript`, `ax-entire.txt`, `conf-shape.txt` (serial masked; no access code), `ssdp.txt`, `ssdp-120.txt`, `tools/conf_shape.py`, `tools/ssdp_listen.py`, `tools/uvprobe.py`, `bs/` (sparse BambuStudio clone), `web/obapi/*`, `web/pybambu/*`, `web/Process.cpp`.

## Track 7 - Time and job fields, and the watch log (2026-10-21)

Source: the live X2D report (firmware `ver 20018`, `shambu printer raw`, 2026-10-21), Studio's MQTT parser
(`DeviceManager.cpp` v02.08.02.61: `mc_remaining_time` :3081, `mc_percent` :3060, `layer_num` :3199, `stg` :3314,
`stg_cur` :3324, `spd_mag` :3304; stage names `get_stage_string` :124-283) and `StatusPanel.cpp` :5524-5534 (speed
levels). shambu reads these into `Status` (`printer.py`).

| Key | `Status` field | Meaning |
| --- | --- | --- |
| `mc_remaining_time` (duplicate `remain_time`) | `remaining_min` | The printer's remaining minutes. On the E7a print it equalled Studio's M73 `R` at every poll (ratio 1.000 to 1.002). On the D.7 print it ran at 0.74 to 0.81 x M73 `R` and still predicted the end to within 9 min. When it departs from M73 `R`, the print is not running at the pace Studio simulated. |
| `mc_percent` (duplicate `percent`) | `percent` | Equals M73 `P` (6 of 7 E7a polls; the seventh was 1 ahead). |
| `spd_lvl` | `speed_level` | Speed level: 1 silent, 2 standard, 3 sport, 4 ludicrous. Live: 2. Not logged on D.7, the likely cause of its 0.75. |
| `spd_mag` | `speed_percent` | The speed level's percent. Live: 100. Community figures (not official): silent 50, sport 124 to 125, ludicrous 166. |
| `stg` | `stages` | The startup stages this job runs, as a list of ids. Live: `[29, 2, 13, 74, 11, 73, 75, 4, 8, 14, 54, 1, 3, 255, 51]`. |
| `stg_cur` | `stage` | The current stage; 0 = printing. |
| `task_id` (else `subtask_id`) | `task_id` | The job id. Also present: `subtask_id`, `job_id`, `project_id`, `profile_id`, `model_id`, `print_type` (live: "cloud"), `plate_idx`. |
| (none) | `read_at` | shambu's own UTC stamp (`2026-10-21T14:23:55Z`), set when the report is decoded. |

The 15 stage ids of the live job, with Studio's text and what toggles each (from `M1002 gcode_claim_action` in the
X2D start G-code; research C):

| Id | Studio's text | When it runs |
| --- | --- | --- |
| 29 | Cooling chamber (`M191 S0`) | when `overall_chamber_temperature < 40` |
| 2 | Heatbed preheating | always |
| 13 | Homing toolhead | always |
| 74 | Heatbed surface foreign object detection | always in the G-code |
| 11 | Identifying build plate type | always in the G-code |
| 73 | Build plate alignment detection | always in the G-code |
| 75 | Heatbed underside foreign object detection (`G3811`) | when `max_print_z >= 145` |
| 4 | Changing filament | always |
| 8 | Calibrating dynamic flow (`M983.3`) | the send option `flow_cali` (`extrude_cali_flag`) |
| 14 | Cleaning nozzle tip | always |
| 54 | Waiting for heatbed to reach target temperature | always |
| 1 | Auto bed leveling (`G29`) | the send option `bed_leveling` (`g29_before_print_flag`) |
| 3 | Vibration compensation (`M970.3`, `M974`) | always in the X2D start G-code |
| 255 | (Studio's table has no text for 255) | in the live list |
| 51 | Printing calibration lines | always |

Stage 39 (nozzle offset calibration, `auto_cali_toolhead_offset_flag`) was not in the live list. The toggles are
print-job options sent with the job, not 3MF settings. shambu never sends them (read-only); it can only log `stg`.

**No start time and no total estimate.** The report has no `gcode_start_time`, start timestamp or total. The only
keys with "time" in the name are `mc_remaining_time` and `remain_time`; `job.stage[].est_time` and `2D.bs.total_time`
are 0 on this firmware. Studio reads no start time either. So the watch log stamps each poll itself, and it bounds
the start by the first RUNNING poll and the end by the first FINISH poll.

**`shambu printer watch --log FILE [--gcode3mf PATH [--plate N]] [--every S] [--max-errors N] [--max-polls N] [--wait-for-start]`.**

- **Poll lines.** One JSON line per poll: `read_at`, `task`, `task_id`, `state` (the raw `gcode_state`), `percent`,
  `layer`, `total_layers`, `remaining_min`, `speed_level`, `speed_percent`, `stage`, `stages` and `print_error`. The
  last poll (FINISH, FAILED or IDLE) carries `"final": true`.
- **Failed reads.** A failed read is a line with a `category` (timeout, tls, login, discovery, identity, report,
  connect, setup, other) and that category's fixed `error` text. It never quotes the exception, because the lower
  layers name the IP, the host and the masked serial. The watch goes on until the print leaves RUNNING/PAUSE or N
  reads in a row fail (default 10, then exit 3). After a failure, the next poll keeps the interval of the last good
  poll.
- **Interval.** `--every`, but at most 60 s while preparing, during the first 15 minutes of RUNNING, and while fewer
  than 15 minutes remain. The start window applies only when the watch saw the start: a PREPARE poll, or a first
  RUNNING poll before layer 2 that is the first poll or follows IDLE, FINISH or FAILED. No sleep follows the last
  permitted poll.
- **`--wait-for-start`.** A watch that finds the printer IDLE, or still showing the last job's FINISH, keeps polling
  every 60 s until a job starts. It is bounded by `--max-polls`. Without the flag, the watch stops at once.
- **`--gcode3mf`.** Writes a header line with the file's name (not its path) and its M73 track, so the log is tied to
  the file it watched. The plate G-code is streamed from the zip, and no other entry is read. A project sent from the
  GUI holds no G-code: export the plate with Export Plate Sliced File and pass that `.gcode.3mf`.
- **Sessions.** Each watch run appends its own header, so a log can hold several sessions. `shambu calibrate` fits
  each session against its own track.
- **Identifiers.** The log never holds the IP, the serial (masked or not) or the access code. `printer status
  --json` shows the IP as `<ip>`, only the masked serial, and `ends_at` as a dated UTC time. `printer raw` drops
  `_topic` (it names the full serial), `net`, `wifi_signal`, `ipcam` (its URL holds the IP) and serial keys. It
  redacts secret-looking keys and any text that names the IP or the serial.
- **Errors.** A log that cannot be opened, written or closed is exit 3. A Studio config problem (no printer
  selected, no access code) is one `setup` line, and the watch stops at once with exit 3.

---

## Track 8 - Printer profiles and the credentials file (2026-10-27)

Why: on 2026-10-27 the X2D went to LAN-only mode on its touchscreen and regenerated its LAN access code (one
observation). Studio's config still held the old code, ARP had no entry for the printer, and shambu had nowhere to keep
an IP or a code. The design and its history: `docs/specs/2026-10-27-printer-profiles.md`.

### The config dir and the two files

The dir is `$SHAMBU_CONFIG_DIR`, else `$XDG_CONFIG_HOME/shambu`, else `~/.config/shambu` (`conf.config_dir()`). A value
that is empty or relative after `~` expansion counts as unset, so no file lands in a checkout. shambu creates the dir
0700 and writes each data file 0600 and atomically, under a lock: `printers.json.lock` and `credentials.json.lock`
sit beside the data files, empty, created with the default umask (not 0600). A symlink or anything that is not a
regular file at either data file's path is refused. `calibration.json` stays under Application Support: it is data, not config.

`printers.json` (`profiles.py` has the schema; never a code):

```
{"version": 1, "default": "x2d",
 "printers": [{"name": "x2d", "serial": "<serial>", "ip": "<ip>", "mode": "lan" | "cloud" | "unknown",
               "insecure": false, "added": "2026-10-27T10:57:00Z", "verified": "2026-10-27T10:57:00Z"}]}
```

`verified` is the time of the last successful `login`. The model, the firmware and where the code is kept are not
stored. A name holds no serial, mask, IP or known code.

`credentials.json` (`vault.FileVault`; never a name, an IP or a path):

```
{"version": 1, "codes": {"<serial>": "<code>"}}
```

A file with a group or other permission bit, or one that another user owns, is refused. The threat model: the same
protection as Studio's own plain-text config against another process of the same user, with a tighter mode, outside
any repository.

### Both modes, one transport

The local broker on 8883 accepts `bblp` and the access code in cloud mode as well as in LAN-only mode. Evidence: shambu
read the X2D on 2026-10-16 while the job reported `print_type: cloud` (Track 7), and read it again on 2026-10-27 at
10:57 UTC in LAN-only mode. Status pushes are outside Authorization Control by Bambu's own list (16 Jan 2025 post), so
neither mode needs Developer Mode for a read. This answers the open question in Track 3. The mode changes only where a
fresh code comes from and how the printer is found. shambu does not detect the mode: SSDP heard nothing in either mode
(Track 3, and again on 2026-10-27), so `login --mode lan|cloud` records what the touchscreen shows, else `unknown`.

### What Studio keeps, and why shambu never writes it

Studio master `79df7ae` (13 Oct 2026):

- `BambuStudio.conf` holds the code as `access_code.<serial>` and the IP of a LAN-mode printer as
  `user_access_dev_ip.<serial>`, encoded against `slicer_uuid` (`DeviceManager.cpp:466-535`). For a cloud printer it
  stores no IP, and its code is whatever the cloud bind list last said (`DevManager.cpp:883-887`).
- Studio rewrites the whole file every 5 s while its config is dirty and again on exit (`GUI_App.cpp:2999-3000`,
  `:3716-3721`), so a write from outside lasts only while Studio is closed.
- After a refused login Studio clears the code it held (`GUI_App.cpp:2280-2290`), so Studio's config cannot be the
  source after a rotation in LAN-only mode until the printer is bound again in the Device tab.
- Studio's manual IP dialog refuses a cloud-mode printer (`ReleaseNote.cpp:2050-2080`).

shambu reads Studio's config as the last code source (after a one-shot flag and the credentials file) and never
writes it. `login --from-studio` copies Studio's selected serial and code into a profile; nothing copies the other way.

### `get_access_code`: never sent

Studio publishes `{"system":{"command":"get_access_code"}}` on every connect (`DeviceManager.cpp:1267,2632`), and the
printer answers with the code. It would heal a rotated code, but it is not `pushall`, and shambu publishes nothing
else. So a refused login is always handed back to the user.

### What `shambu printer login` does

1. Checks the flags (usage, exit 2): `--from-studio` takes no `--serial`, code flag or `--printer`; a new printer needs
   `--ip`; `--serial` must match the named profile; `--insecure` on a new printer needs `--serial` (the certificate
   is unreadable without verification).
2. Reads the code: `--access-code-file FILE` or `--access-code-stdin` (the first line), else the prompt `LAN access
   code (not shown): ` when stdin and stderr are terminals, else usage. `--from-studio` takes Studio's selected serial
   and its code instead. A code must be 4 to 128 characters of letters, digits and `._:@+=/-`, the first a letter or
   a digit (`vault.safe_code`).
3. Finds the serial: Studio's (`--from-studio`), the profile's (`--printer NAME`, or the default with no `--ip`), else
   `--serial` or the CN of the certificate at `--ip` (`certificate_serial`: one TLS handshake against Studio's CA
   bundle, no MQTT, no credential). A `--serial` that differs from the CN fails, and nothing is written.
4. Validates the draft profile (name, clash) before any read.
5. Reads the printer once with the code: at `--ip`, else at the profile's IP, else (only `--from-studio` without
   `--ip`) after discovery (ARP, then the default-route /24); TLS verified, MQTT CONNECT, one `pushall`, a full report.
   A refusal is exit 3 and writes nothing.
6. Under the profiles lock: stores the code in `credentials.json`, then writes the profile with `verified` set and the
   IP the read used. If the profile write fails, the previous code (or none) is put back.
7. Prints `profile x2d: serial <masked>, code stored, mode lan, read at HH:MM UTC`, plus ` (default)` when it is the
   default. The first profile becomes the default; `--default` moves it.

A refused login keeps `refused the MQTT login` (the watch log's `login` category keys on it), then names the code's
source and the profile, then the next step for the profile's mode: LAN-only, read the code off the touchscreen and
run `shambu printer login`; cloud, open Studio once so it fetches the code; `unknown`, both. The printer can
regenerate its code when LAN-only mode is toggled, when the refresh icon next to the code is pressed, after some
firmware updates and sometimes after a reboot; which of these always does it is not settled (spec section 8).

`login --insecure` stores the TLS-off choice on the profile (`"insecure": true`), and every later read through that
profile skips the check. A `login --printer NAME` refresh without `--insecure` clears it. For a new printer,
`login --insecure` needs `--serial`.

### Reads through a profile, and when the subnet pass runs

A read through a profile goes to the profile's stored IP and does not search; `login` always stores the IP it read
from. The subnet pass (the default-route /24 after the ARP table) runs in `printer discover` (always), in `login` when
it has no IP (`--from-studio` without `--ip`), and on a profile read only when the profile has no IP, which `login`
never leaves. A read without a profile and without `--ip` uses the ARP table only, as in 0.4.0.

When the printer's address changes (a new DHCP lease), a profile read fails with `cannot connect ... The profile x2d
expected this address (last login YYYY-MM-DD HH:MM UTC); run: shambu printer login --printer x2d --ip <new ip>, or
shambu printer discover`. The profile is never rewritten by a read: `printer discover` finds the new address and a
`login --printer NAME --ip <ip>` records it.

### Trust boundary

`login --ip` without `--serial` binds the profile to whatever Bambu-signed certificate answers at that IP. A
chain-valid CN proves a Bambu device, not the intended one. The CN must be serial-shaped and the MQTT read that follows
pins that serial; `--serial` from the touchscreen is the stronger binding. After enrolment, discovery with a known
serial and TLS verified accepts only a host whose certificate names that serial, and no code goes to a host before
that check.
