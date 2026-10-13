# 05 - bambu-cli (davglass) ideas

> **Provenance and licence notice.** Bambu Studio is free software under the AGPL-3.0
> (https://github.com/bambulab/BambuStudio). The findings in this document come from reading that source at tag
> `v02.08.02.61`, inspecting the shipped macOS binary (symbols, strings, crash reports, a debugger session on a local
> re-signed copy) and running experiments with its command line. Short excerpts are quoted for interoperability only.
> No Bambu Studio code is included in shambu, and shambu is not affiliated with or endorsed by Bambu Lab.
> See `DISCLAIMER.md` at the repository root.

Source: https://github.com/davglass/bambu-cli, shallow clone at `bambu-cli-re/repo`, HEAD `d02dc27` (2025-01-28), v1.0.17, archived, 94 stars. About 2,300 lines of Node.js in `bin/cli.js` + 16 files in `lib/`. I read every source file. I did not run `npm install` or execute any of its code. No code is reused here, only ideas.

Verdict in one line: the parts worth copying are the HMS decoder, the `slice_info.config` / `plate_N.json` / gcode-header 3MF reader, the fuzzy machine selector, the `mqtt --path` JSONPath debugger and the "which printer has the right filament" check. Its status model is X1C-only and pre-2025, so it is stale for the X2D.

---

## 1. Inventory

Command dispatch: `bin/cli.js` uses yargs. Positional arg 1 is the command, arg 2 is a machine id or name. A command can be any unique prefix (`s` -> `status`, `con` -> `config`). If no `machines` key is in the config, any command becomes `login`.

Global flags: `--debug` (or `DEBUG` env), `--redact`, `--json`, `--id`, `--yes`, `--force`, `--filter`, `--download[=dir]`, `--delete`, `--parse`, `--file`, `--upload`, `--keys`, `--path`, `--set`, `--value`, `--ip`, `--slim`.

| command | purpose | protocol | read-only or control |
|---|---|---|---|
| `login` | Prompt for bambulab.com user and password, get cookie token, list bound devices, discover LAN IP | HTTPS `POST https://bambulab.com/api/sign-in/form` (JSON `{account,password,apiError:""}`, tokens from `Set-Cookie: token=`/`refreshToken=`); `GET https://api.bambulab.com/v1/iot-service/api/user/bind` (Bearer) -> `dev_id, name, dev_access_code, dev_model_name, dev_product_name`; cloud MQTT `mqtts://us.mqtt.bambulab.com:8883` user = JWT `username` claim, password = token, sends `pushall`, reads IP from `print.ipcam.rtsp_url` hostname; falls back to manual IP prompt | read-only (cloud account) |
| `config [--set k --value v]` | Print the config JSON, or set a top-level key (`"null"` deletes, `"true"/"false"` coerced) | local file | local write |
| `machines` / `ls` | Table of id, name, IP, access code | local file | read-only |
| `set-ip <machine> --ip a.b.c.d` | Set a machine IP by hand (IPv4 regex check) | local file | local write |
| `status [machine] [--json] [--slim]` | Per printer: FTP up, MQTT up, AMS units, nozzle, stage, task, percent, remaining, speed, external spool. Single printer adds firmware table, firmware-update warning, ETA, AMS tray table, HMS errors with wiki URL | FTPS `LIST /` (port 990, implicit TLS, user `bblp`, pass = access code) as liveness; LAN MQTT `mqtts://<ip>:8883` user `bblp`, subscribe `device/<id>/report`, publish `get_version` + `pushall` to `device/<id>/request`; ends after more than 2 `print` messages and at least 1 `info` message. Writes AMS filament snapshot back to config | read-only (except config cache write) |
| `files [machine] [--filter s] [--download[=dir]] [--parse] [--delete] [--yes]` | List files in FTPS root whose name has `.gcode` (so `.gcode.3mf` and `.gcode`), sorted, with date, size, totals; can download, parse after download (into tmp, then delete), or delete | FTPS `LIST /`, `RETR`, `DELE` | read-only (list, download) / control (delete) |
| `timelapse [machine] [--filter] [--download] [--delete] [--yes]` | Same table for `/timelapse`, names containing `video_`; delete also removes `/timelapse/thumbnail/<name>.jpg` | FTPS | read-only / control (delete) |
| `parse <file.3mf> [--force]` | Read a local 3MF: per plate name, bed type, nozzle, filament colours/types, metres, grams, gcode line count, estimated time. Then match plate filaments against printer AMS snapshots | local zip read, cached by MD5 in the XDG cache dir | read-only |
| `upload [machine] --upload <file or dir>` | Upload one `.gcode*` file, or every `.gcode*` file in a directory, to FTPS root with a progress bar | FTPS `STOR /<basename>` | control |
| `mqtt [--json] [--keys [a,b]] [--path <jsonpath>]` | Live MQTT tail for every machine: one summary line per message (`[id] (topic): command (keys: N)`), or full JSON, or top-level keys, or a JSONPath query | LAN MQTT subscribe only (no pushall) | read-only |
| `print --file` | Hidden (commented out of yargs, but `commands.print` still dispatches). Publishes `project_file` | LAN MQTT publish | control |
| `completion` | yargs shell completion script | none | n/a |

`bin/fetch-hms.js` is a build step, not a command. CI runs it to refresh `data/hms.json` from `https://e.bambulab.com/query.php?lang=en` (926 `device_hms` entries plus a `device_error` table, version 202501221030).

## 2. Config model

- **Path:** `xdg-app-paths` with name `bambu-cli`, file `config.json`. Linux: `~/.config/bambu-cli/config.json` (issue #2 confirms this). macOS and Windows follow that library's platform defaults (I did not verify the exact macOS path). Parse cache: XDG cache dir, `<md5>.json` per 3MF.
- **Shape:**
  ```json
  { "username": "", "mqtt_user": "", "slim": false,
    "machines": [ { "id": "<serial>", "name": "", "token": "<access code>", "ip": "",
                    "model": "BL-P001", "make": "X1 Carbon",
                    "ams": 2, "filaments": [ {"color":"FFFFFF","type":"PLA"} ],
                    "external": {"color": false, "type": false} } ] }
  ```
  `id` is the serial (MQTT topic key). `token` is the LAN access code. `ams`, `filaments`, `external` are a cache that every `status` run rewrites.
- **Selection:** positional arg 2 is matched case-insensitively as a substring against `id` and `name` of every machine. One match -> use it. Several -> print them and exit 1. No arg and exactly one machine -> default to it. No arg and several machines -> `status` and `mqtt` run on all of them. `files`, `upload`, `timelapse` need a machine.
- **Multiple printers:** fan-out with `async.each`, FTP pass then MQTT pass. The table hides ID and IP columns with `--slim` (config key `slim` sets it by default). There is no named default machine and no `--machine` flag (`--id` exists).
- **Secrets:** access codes are stored in plain text and shown by `ls`. `--redact` masks name, id, IP, access code, home dir and usernames in all log output (keeps first 2 and last 2 characters, same length so tables stay aligned).

## 3. UX patterns worth copying

- **Prefix commands and fuzzy machine match** with an explicit "ambiguous, here are the candidates" error. Good for an agent: deterministic, fails loud.
- **`--json` on status** prints the full internal state object instead of the table. Weakness: it dumps the raw merged MQTT state plus the config, not a stable schema. Copy the flag, not the shape.
- **`mqtt` debug tail** with `--keys` (list top-level keys, or filter to some) and `--path` (JSONPath). This is the most useful thing for reverse-engineering new firmware fields on the X2D.
- **Status termination rule:** wait for `info` once and `print` more than twice, then disconnect. A bounded snapshot, not a watch loop. There is no watch or follow mode.
- **Liveness split:** separate FTP and MQTT check marks per printer, so "MQTT down but FTP up" is visible.
- **File table:** sorted, filter by substring, totals row (count and size), download skips files whose local size matches. Download target must be a directory.
- **Timelapse:** list `/timelapse/video_*`, thumbnails at `/timelapse/thumbnail/<same>.jpg`. No camera stream handling at all (RTSP URL used only for IP discovery).
- **Filament/AMS display:** AMS letters A, B (unit index -> letter), tray `A1..A4`, global tray index `n -> AMS floor(n/4), slot n%4+1`, active tray marked `*` from `ams.tray_now`, `254` = external spool. Colour swatch in terminal, Pantone-ish colour name, `tray_info_idx` -> brand name table, `remain` %.
- **Errors:** `[ERR!]` and `[WARN]` go to stderr. Exit codes 1 (bad input) and 2 (machine not found). HMS lines get a severity from the code and a clickable wiki URL.
- **Confirmation:** `--delete` asks `[y/N]`, default no, `--yes` skips. `--download --delete --parse` together is refused.
- **Parse cache by MD5** so re-parsing a large 3MF is instant; `--force` bypasses.

## 4. Protocol details

**LAN connection.** MQTT `mqtts://<ip>:8883`, user `bblp`, password = access code, TLS verify off, fixed `clientId: "bambu-cli"` (bad: two runs kick each other off; use a random suffix). FTPS port 990 implicit TLS, same credentials, cert check off.

**Init payloads** (`lib/const.js`, credited to ha-bambulab), published to `device/<serial>/request`:
```json
{"info":    {"sequence_id": "0", "command": "get_version"}}
{"pushing": {"sequence_id": "0", "command": "pushall"}}
```

**Fields it parses** (`utils.mqttMessage`):
- `info.module[]` -> `name`, `sn`, `sw_ver` (machine firmware where `sn == serial`, `ahb` = AMS hub, `ams/N`).
- `print.hms[]` -> `attr`, `code`.
- `print.subtask_name` (strips `.gcode.3mf` / `.gcode`; `auto_cali_for_user_param.gcode` -> "Auto Calibration").
- `print.stg_cur` -> stage table (-1 Idle, 0 Printing, 1 Auto bed levelling ... 35 Nozzle clog pause; full list in `lib/const.js`).
- `print.nozzle_diameter`, `print.mc_remaining_time` (minutes), `print.mc_percent`, `print.spd_lvl` (1 Silent, 2 Normal, 3 Sport, 4 Ludicrous).
- `print.ams` -> `ams[]` (`id`, `temp`, `tray[]`), `tray_now`; per tray `id`, `tray_color` (RRGGBBAA, first 6 used), `cols[]` (multi-colour), `tray_type`, `tray_info_idx`, `remain`. An empty tray is an object with only `id`.
- `print.vt_tray` -> `tray_type`, `tray_color` (external spool).
- `print.upgrade_state.ota_new_version_number`.
- `print.ipcam.rtsp_url` (IP discovery, cloud path only).

**Not parsed:** `gcode_state` (it never maps it; it uses `stg_cur` for "Printing" and resets fields when stage is Idle), `layer_num`/`total_layer_num`, any temperature, `print_error`, `wifi_signal`, `lights_report`. It also merges partial delta messages naively: a delta without `stg_cur` sets `printing` to `undefined`. A read-only tool must merge deltas into a full state dict and only overwrite keys that are present.

**ETA:** `eta = now + mc_remaining_time * 60 s`, shown as local time. Remaining text is a humanised duration.

**HMS decoding** (`lib/utils.js`):
- `attr` and `code` are integers. Each is formatted as 8 hex digits, upper case, split 4+4.
- Display code: `HMS_AAAA_AAAA_CCCC_CCCC`.
- Lookup key in `hms.json`: the 16 hex digits concatenated (`AAAAAAAACCCCCCCC`) against `data.device_hms.en[].ecode`.
- Severity: `code >> 16` -> 0 log, 1 fatal/error, 2 serious/warn, 3 common/info, 4 info.
- Wiki URL: `https://e.bambulab.com/index.php?e=<16 hex>&d=<serial>&s=device_hms&lang=en`.
- Source table: `https://e.bambulab.com/query.php?lang=en` (also has `device_error` for `print_error` codes, which it does not use).

**FTPS paths used:** `/` (prepared `.gcode.3mf` and `.gcode` files), `/timelapse` (`video_*.mp4`, or `.avi` on some models), `/timelapse/thumbnail/*.jpg`. It never touches `/cache`, `/model` or `/image`.

**3MF handling** (`lib/parse.js`, streaming unzip):
- `Metadata/model_settings.config` (XML): per `<plate>`, metadata `plater_id`, `plater_name`.
- `Metadata/slice_info.config` (XML): per `<plate>`, metadata `index`; each `<filament>` attributes `type`, `color`, `used_m`, `used_g`, `tray_info_idx` (it drops `id`).
- `Metadata/plate_N.json`: `bed_type` (`cool_plate`, `eng_plate`, `hot_plate`, `textured_plate`), `nozzle_diameter`.
- `Metadata/plate_N.gcode`: line count, and the header line containing `total estimated time` (Bambu writes `; model printing time: Xh Ym; total estimated time: Xh Ym`; it splits on `; ` then `: `).
- Plates with only one key are dropped (unsliced). A file without `.gcode` in the name is flagged "Simple Project File (not ready to print)".
- No thumbnails (`Metadata/plate_N.png`), no `project_settings.config`, no printer model check, no `prediction`/`weight` metadata from `slice_info`.
- **Printer match:** for each plate, set of `type:::RRGGBB` must be present in the printer's cached AMS snapshot. If not, it suggests the nearest colour of the same type via a colour-difference metric and names the AMS slot.

**Control payload (note only, out of scope):** `print.command = "project_file"`, `param = "Metadata/plate_1.gcode"`, `url = "ftp:///<file>"`, flags `timelapse`, `bed_levelling`, `flow_cali`, `vibration_cali`, `layer_inspect`, `use_ams`, `ams_mapping` (hard-coded tray indices). Never wired to a command.

## 5. Known issues and why it was archived

- **Archive reason (README):** "Bambu Labs has made it very clear they do not want users to interact with their printers. You can no longer log into their website which is needed to use this system." The whole tool depends on cloud login to learn serial, access code and IP. Last commit 2025-01-28, the same month as Bambu's Authorization Control firmware announcement.
- **#8 (open):** login now returns a Cloudflare "Just a moment..." HTML page, which crashes `JSON.parse`.
- **#5 (open):** no 2FA support; a fork that added it then hit Cloudflare 403.
- **#6 / #4:** crash `machines.length` of undefined on first run; fixed in the repo (v1.0.16) but npm shipped stale code.
- **#3:** IP discovery hangs when "LAN Mode Liveview" is off, because the IP comes only from `ipcam.rtsp_url`; fixed with a manual prompt and `set-ip`.
- **#2:** A1 mini AMS Lite crash on missing `tray.cols`.
- **#7:** feature request to batch-edit 3MF text (not addressed).
- Only tested on two X1C printers with 2 AMS each.

**Stale for 2025-2026 firmware:**
- Cloud login is dead. Our tool must take serial, IP and access code by hand (they are on the printer screen), with no cloud call.
- Authorization Control: with newer firmware, third-party control over LAN MQTT needs LAN-only mode plus Developer Mode. Read-only report subscription with the access code is what tools like ha-bambulab still rely on, but this must be checked on the X2D's actual firmware before we depend on it.
- Fixed `clientId` and `pushall` on every run: newer firmware and the P1/A1 lines rate-limit `pushall`. Prefer subscribe and wait for the periodic full report, with a single `pushall` only if needed.
- Dual nozzle (H2D, and the X2D per our brief): newer reports carry per-extruder data (on H2D, a `device.extruder.info[]` array with per-nozzle temperature and state, `device.nozzle.info[]` for diameter and type, and AMS units mapped to an extruder). The single `nozzle_diameter`, single `vt_tray` (H2D uses a `vir_slot` list) and `tray_now` assumptions in this tool break there. Field names for the X2D must be captured live with a `mqtt --keys` style dump, not trusted from this repo.
- `stg_cur` table stops at 35; newer firmware adds stages and uses `gcode_state` (`IDLE`, `PREPARE`, `RUNNING`, `PAUSE`, `FINISH`, `FAILED`, `SLICING`) as the primary state.
- `hms.json` is frozen at version 202501221030; the X2D needs a fresh fetch.
- 3MF: newer Bambu Studio versions add per-extruder filament mapping and nozzle info to `slice_info.config` and `project_settings.config`; the parser ignores them.

## 6. Features to adopt in our Python bambu tool

Ranked by use for: prepare .3mf with Bambu Studio CLI, inspect/patch 3MF, read printer status over LAN MQTT (read-only).

| # | Feature | Mode | Why, and what to change from bambu-cli |
|---|---|---|---|
| 1 | 3MF plate summary: per plate name (`model_settings.config`), bed type and nozzle (`plate_N.json`), filaments with `type`, `color`, `used_m`, `used_g`, `tray_info_idx` (`slice_info.config`), estimated time from the `plate_N.gcode` header | read-only | The verification step after a Bambu Studio CLI slice. Also read `prediction` and `weight` from `slice_info` plate metadata, `printer_model_id` and `nozzle_diameters`, and per-extruder fields for the X2D. Output JSON first. |
| 2 | "Is this a sliced file?" check (plates with gcode vs project only) | read-only | Catches a CLI run that exported a project without slicing. Check for `Metadata/plate_N.gcode` and its MD5 entry, not the filename. |
| 3 | HMS decoder: 8+8 hex code, `code >> 16` severity, lookup in a vendored `hms.json`, wiki URL | read-only | Turns `print.hms[]` into readable errors. Add a refresh command for the table and also decode `print_error` via `device_error`. |
| 4 | Status snapshot with a bounded wait rule (exit after a full report), `--json` with a stable schema | read-only | Agent-friendly. Merge deltas into one state dict. Use `gcode_state` as the primary state, `stg_cur` as detail, `mc_percent`, `mc_remaining_time` -> ETA (`now + min*60`), `layer_num/total_layer_num`, temperatures per extruder. |
| 5 | Raw MQTT inspector: `--keys`, `--keys a,b`, `--path <jsonpath>`, bounded by count or seconds | read-only | The tool to learn the X2D's real report schema. Subscribe only, never publish except an optional single `pushall`/`get_version`. |
| 6 | AMS table: unit letter, slot, colour hex, multi-colour `cols`, `tray_type`, `tray_info_idx` -> filament name, `remain`, active marker from `tray_now` (254/255 = external) | read-only | Lets the agent check what is loaded before preparing a project. Extend for per-extruder AMS mapping. |
| 7 | Plate-vs-loaded-filament check, with nearest-colour suggestion | read-only | Before slicing, warn if the project's filaments do not match the AMS. Compare type plus colour, report the closest slot. Read-only advice only; never write `ams_mapping`. |
| 8 | Machine config: serial, IP, access code, name, model; fuzzy selection by substring of id or name; single machine is the default; ambiguous match fails with candidates | read-only (local file) | No cloud login. Keep the access code out of normal output; copy `--redact`. Use a random MQTT client id. |
| 9 | Firmware table from `get_version` (`info.module[]`: `name`, `sn`, `sw_ver`) and the OTA-available warning | read-only | Records firmware next to each status read, which matters for schema drift. |
| 10 | FTPS file listing and download (`/`, `/cache`, `/timelapse`, thumbnails), with size-match skip | read-only, but borderline | Useful to pull back a sliced file or a timelapse. Treat as optional: it opens a second protocol, and the brief is MQTT-only status. |
| 11 | MD5-keyed parse cache with `--force` | read-only | Cheap speed-up for large 3MFs. |
| 12 | Liveness columns (MQTT reachable, auth OK) per printer | read-only | Cheap, makes "wrong access code" vs "printer off" clear. |

**Skipped, control (note only):** `upload` (FTPS `STOR`), `files --delete` / `timelapse --delete`, `print` (`project_file` with `ams_mapping`), cloud `login`. All are out of scope. The one pattern to remember from them: destructive actions ask `[y/N]` with default no, and `--yes` is the only bypass.
