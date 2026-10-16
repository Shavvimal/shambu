# Error status: the project did not use the filament loaded on the printer (2026-10-16)

Status: OPEN. Severity: high (a sliced project that reaches the owner with the wrong filament, and a GUI swap that silently drops the process preset). Owner's verdict: "this is an oversight, not a feature". Reported by the owner from the plate-9 bracket session; written up by the assistant that built the plate.

## 1. What happened, in order

1. 2026-10-13: `shambu doctor` was run. The GUI's last-used filament was `Bambu PETG Basic @BBL X2D 0.4 nozzle`, colour `#00AE42` (green). A plate spec was written with that preset and colour. The plate was built, sliced and printed. Correct at the time: that spool was on the left nozzle.
2. 2026-10-16: the owner changed spools. Left external spool: Generic PETG, white. Right external spool: Bambu PETG Basic, blue. Bambu Studio's own filament list shows exactly that (dropdown: "Left filaments: Ext Generic PETG", "Right filaments: Ext Bambu PETG Basic").
3. 2026-10-16, 15:30: a new bracket revision was built from the 13 October spec without running `doctor` again. The project carried `Bambu PETG Basic`, green, on filament 1.
4. The owner opened the `.gcode.3mf` in Studio. Studio noticed the project's filament did not match the loaded one and offered the swap. The owner picked the loaded Generic PETG.
5. The swap reset the process preset to the stock `0.20mm Standard @BBL X2D` (2 walls, 15 % infill). Studio re-sliced: 5 h 08 min, 115.8 g. The headless slice of the project was 6 h 34 min, 175.7 g (6 walls, 40 % gyroid). The owner's screenshot of the sidebar showed the wrong figures; the assistant caught the mismatch only because the headless numbers were in the report.
6. The plate was rebuilt with `Generic PETG @BBL X2D 0.4 nozzle`. `doctor` now reported that preset, but the stored colour was still `#00AE42`, so the project opened green while the spool is white. Rebuilt once more with the colour pinned to `#FFFFFF`. Three builds and about 20 minutes to reach a project that opens clean.

## 2. Root cause, three layers

1. **The tool reads Studio's memory, not the printer.** `conf.py` takes the GUI's last-used presets and colours from `BambuStudio.conf` (`presets.filaments`, `presets.filament_colors`). Those are whatever the GUI last had selected, which can be days old and can differ from both spools on the printer. `printer.py` talks to the printer over MQTT but only reads state, progress, temperatures and HMS; it ignores the `ams` and external-spool trays in the same `pushall` report, which carry the loaded filament type, colour and preset id per slot.
2. **The checker never compares.** `shambu check` validates presets against the installed preset files and checks placement and reach. It does not compare the spec's filament or colour with the GUI's presets, let alone with the printer's trays. A spec written for one spool passes every check after the spool changes.
3. **The skill relies on the operator.** The skill's "Before you start" says to run `doctor` and use its values. That was done once per session instead of once per build. Nothing in the tool enforced it, and the skill text does not say "before every build".

Studio's behaviour (a filament swap in the GUI reverting the process to a system preset) is outside the tool, but the tool invited it by shipping a project whose filament could not be printed as-is.

## 3. What the printer reports and the tool ignores

One `pushall` answer holds, under `print`:

- `ams.ams[].tray[]`: for every AMS slot, `tray_type` (material, e.g. `PETG`), `tray_color` (RRGGBBAA hex), `tray_sub_brands` (e.g. `PETG Basic`), `tray_info_idx` (Bambu's filament preset id, e.g. `GFG00` for Bambu PETG Basic, `GFG99` for Generic PETG), `remain`, `k`.
- The external spool(s): single-nozzle printers report `vt_tray` with the same fields; the dual-nozzle X2D/H2D family reports the two external spools as virtual trays (ids 254 and 255 in the captures seen in the community; the left/right assignment must be confirmed on this printer, see section 6).
- `ams.tray_now` / the per-nozzle "now" fields: which tray is loaded in each nozzle at this moment.

The installed filament preset files (which `presets.py` already reads) carry `filament_id` / `setting_id` values that match `tray_info_idx`, so the loaded tray can be resolved to the exact preset name the spec needs (`Generic PETG @BBL X2D 0.4 nozzle`) without guessing from the material string.

## 4. Required behaviour (the owner's words: "query the printer whenever opening in Bambu and use the settings loaded in Bambu already")

1. **`shambu printer filaments [--json]`** (new, read-only, the same one-`pushall` path as `printer status`): for each nozzle (left = filament 1, right = filament 2) and each slot, print the loaded state, material, colour, preset id and the resolved preset name. Nothing is ever sent but the `pushall`.
2. **`shambu check` and `shambu build` query the printer by default.** Before resolving a spec:
   - a filament whose `preset` is omitted, or given as `"loaded"`, takes the filament loaded in that nozzle, with its colour;
   - a filament whose `preset` is pinned is compared with the loaded one: a different preset is a FAIL (exit 1) with a one-line message naming both, unless `--allow-filament-mismatch` is passed and the report records that override; a different colour is replaced by the tray colour unless the spec pins `colour` and says why in a `colour_note`;
   - the report's filament line states the source: "from the printer (left external spool, white Generic PETG, read 15:41)" or "from the spec (override)".
3. **Printer unreachable:** WARN, fall back to the GUI presets from `BambuStudio.conf`, and say so in the first lines of the report and in the hand-back. Never fail silently to the GUI values.
4. **`doctor`** gains a "loaded on the printer" section next to the GUI presets, so a mismatch between the two is visible at a glance.
5. **The skill** changes "run the doctor" to "run `shambu doctor` and `shambu printer filaments` before EVERY build, and write the spec's filaments as `"loaded"` unless the owner names a spool". The hand-back template gets a line: "Filaments: from the printer at HH:MM: 1 = ..., 2 = ...".

## 5. Acceptance

- A captured `pushall` from the owner's X2D with the two external spools and (if fitted) the AMS, stored under `tests/fixtures/`, parsed by a unit test into the per-nozzle table above.
- Regression test for this incident: spec pins `Bambu PETG Basic` green; printer reports left = Generic PETG white; `check` exits 1 with the message; with `"preset": "loaded"` the built project carries `Generic PETG @BBL X2D 0.4 nozzle` and `#FFFFFF`, and `inspect` shows them.
- Fallback test: printer offline, `check` passes with the WARN line present in the report.
- Live test on the bench: build a plate with `"loaded"`, open it in Studio, confirm Studio offers no swap and the process preset survives.

## 6. Open questions to settle on the real printer

- Which virtual tray id is the left external spool on the X2D, and whether the field is `vt_tray`, `vir_slot` or both. Capture one `pushall` with both external spools loaded and note the ids and colours.
- Whether `tray_info_idx` is populated for a Generic spool without an RFID tag (it was selected by hand in Studio or on the printer screen). If it is empty, fall back to `tray_type` + `tray_sub_brands` and map to the Generic preset of that material.
- Whether the preset id resolves to a preset the tool can name on this machine (user presets as well as system ones).

## 7. Interim rule until this is built

Run `shambu doctor` before every build. Confirm the GUI filament presets match the spools on the printer by eye (Studio's filament dropdown lists "Left filaments" and "Right filaments" with the loaded spools). Pin the spec's filament to the loaded preset and the colour to the spool colour. Note both in the report.

## 8. Evidence

- `BambuStudio.conf` read by `doctor` on 2026-10-13: `presets.filaments = ["Bambu PETG Basic @BBL X2D 0.4 nozzle"]`, `filament_colors = "#00AE42"`; on 2026-10-16 after the owner's swap in the GUI: `["Generic PETG @BBL X2D 0.4 nozzle"]`, colour still `#00AE42`.
- Studio's filament dropdown on 2026-10-16 (owner's screenshot): Left filaments "Ext Generic PETG" (white swatch), Right filaments "Ext Bambu PETG Basic" (blue swatch).
- Headless slice of the project: 6 h 34 min, 175.7 g. GUI slice after the swap: 5 h 08 min, 115.80 g, process reverted to `0.20mm Standard @BBL X2D`.
- Plate files: `content/drafts/openarm/g2-redesign/printing-tasks/plates/plate-9-bracket/` in the blog repo (plate-D7.json, the three build logs `bambu/shambu-build-D7b.log`, `-D7c.log`, `-D7d.log`).

## 9. Resolution (2026-10-16, shambu 0.3.0)

Status: FIXED in the tool and the skill; the bench test in section 5 (open a `"loaded"` build in Studio, confirm no swap offer) is still to run.

- Section 4 built as written: `shambu printer filaments`, `check`/`build` read the printer by default
  (`"loaded"`, the FAIL on a pinned mismatch, `--allow-filament-mismatch`, the colour rule with `colour_note`,
  the source on every filament line), the WARN fallback to the GUI presets, the `doctor` section with `MISMATCH`
  lines, and the skill text ("before EVERY build", `"loaded"` by default, the `Filaments:` hand-back line).
- Section 6 settled on the real printer (capture in `tests/fixtures/x2d_pushall_two_external_spools.json`):
  the X2D reports `vir_slot` only, no `vt_tray`; id 254 is the left external spool and 255 the right one, and
  each extruder's `snow` names the tray it holds (left 0xFE00, right 0xFF00). The Generic spool chosen by hand
  did carry `tray_info_idx` GFG99; an empty id falls back to the Generic preset of the material. Both ids resolve
  on this machine to the X2D 0.4 system presets.
- Adversarial review (Codex, gpt-6-astra) before the PR: ten findings, all fixed. The ones that changed behaviour:
  an unfed spool or an unlisted slot is never taken for `"loaded"`; the nozzle count comes from the machine preset,
  not the report; a pinned preset that cannot be compared is a FAIL; the override keeps the colour rule; a bad
  colour is a spec problem; printer error text in the report has the IP redacted; the sweep shows the WARNING and
  the sources; the skill marks an offline build's filaments unverified.
- Section 5: the fixture test, the regression test (pinned Bambu PETG Basic green fails with the message; `"loaded"`
  builds Generic PETG `#FFFFFF` and `inspect` shows it), and the offline fallback test are in
  `tests/test_loaded_filaments.py`. Live: `shambu printer filaments` and `shambu doctor` read the two spools and named
  both presets.
