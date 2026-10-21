# Error status: "three plates" from the checker, and estimates shown as times (2026-10-20 to 2026-10-21)

Status: FIXED in the tool and the skill for 0.4.0 (section 9); the open questions in section 6 stay open. Severity: high (the tool told the owner a five-part layout needs three plates; the owner placed the same parts by hand in Studio with 10 to 16 mm between them and it sliced and printed). The owner's words: "it fits fine, what on earth were you on about?". Written up from the G2 rev E plates session, its post-mortem and the research that corrected the post-mortem.

## 1. What happened, in order

1. 2026-10-20: a plates subagent ran the installed `shambu check`: `PresetError: filament preset 'loaded' not found`, exit 3, three times. The CLI on PATH was 0.2.1 (a `uv tool install .` snapshot of a checkout, taken on 2026-10-13), the plugin's skill text 0.2.0, the checkout 0.3.0. From then on the session ran the checkout with `uv run --project`. Nothing compared the three versions.
2. Plates 11, 11a, 12 and 13 were built and sliced. Plate 12 was turned 45 degrees on the bed diagonal because the support margin refused it straight.
3. 20:26 to 20:43: housing, camera shell, mount and feeler bar on one plate, six specs in a row. Every check failed: `the parts need 2 plates`, `feeler_module_print is 226.4 mm deep (+16.0 margin): deeper than the bed area`, and overlap and bed-edge FAILs on every hand placement. The owner was told "three plates". Two plates were released (13 h 54 min, 379 g; 1 h 30 min, 37 g).
4. The assistant explained this with two claims: "the housing must print alone" and "the feeler needs the bed diagonal". Both were wrong: a plate with the mount beside the housing passed 16 minutes later, and the feeler now prints straight along the bed edge.
5. 20:57: the owner placed housing, shell and mount by hand in Studio: 10 h 36 min, 271 g (Studio's default process).
6. 10-21: the new housing mesh went into the owner's layout by unzipping the 3MF and rewriting its XML by hand (no verb existed). The first run silently kept the old mesh. A cradle was added: 18 h 0 min, 481 g. That plate printed.
7. 09:10: one `doctor` call "returned nothing". It never ran: the call was `timeout 60 uv run ... shambu doctor --json 2>/dev/null | python3 -c "json.load(...)"`, macOS has no `timeout` command, the shell sent "command not found" to `/dev/null`, and `json.load` failed on empty input 0.21 s later. doctor did not hang.
8. 09:13 to 09:21: a two-filament version (PLA interface) never sliced. `check` gave six overlap FAILs plus bed and reach FAILs on the layout now printing. Hand-run Studio calls failed: first `could not found extruder_type ...` (the per-extruder variant keys), then exit 156 on every variant. shambu's own two-part seed failed with -102. The assistant said "the reason is space, not the filament". With the tower off and with three parts removed it still failed, so that claim had no support. The real cause of 156 (found later, section 2): one empty key, `extruder_nozzle_stats: []`, in every unsliced CLI export.
9. Every time shown to the owner was Studio's estimate. The two prints with a progress log disagree. Bracket D.7 (2026-10-16, PETG, a standard-flow HS01 nozzle, the speed level not logged) ran at 0.75 x Studio mid-print and about 0.75 to 0.80 over the whole print (6 h 50 predicted, about 5 h 0 to 5 h 30 real). The E7a cradle (2026-10-21) ran at 1.05 x Studio between layers 135 and 182.

## 2. Root cause, five layers

1. **The checker over-reserved.** `layout.support_margin` grew every supported part by 0.25 x its height (5 to 25 mm) on all four sides, at every height, for any support type. The pair test added both neighbours' margins. Footprints were axis-aligned boxes. The 25 mm came from one measurement (13 to 15.6 mm, one side, below z 10). Studio has no clearance rule between objects before slicing; a real clash is exit -101 after slicing.
2. **The packer failed closed.** A shelf packer with no rotation and rows by depth. When it gave up it left the parts at the origin, and the report printed bed-edge FAILs for positions nobody chose. "Needs N plates" was its answer, not Studio's.
3. **The skill made the checker the judge.** "`check` until every line reads OK", and no rule against stating a cause. The assistant turned the checker's lines into "three plates" and into two causes no output showed. The hand-back template, which would have shown the check lines and no slice, was skipped.
4. **Versions and verbs.** Three versions ran in one session, and the CLI had no verb for mesh replace, add filament, move object or move tower, so the session hand-ran Studio nine times against the skill's rule.
5. **Unsliced exports were not re-sliceable.** The CLI writes `extruder_nozzle_stats: []` on an export without `--slice` (BambuStudio.cpp:6648-6686). A later CLI run reads the present key as authoritative, counts 0 nozzles and throws "No valid nozzle found" (BambuStudio.cpp:4101-4156, ToolOrdering.cpp:1477-1481), mapped to -100, exit 156, with the text on stderr only (BambuStudio.cpp:7413-7418). A one-filament plate of the same export failed the same way: it was never space and never the second filament.

The time numbers are Studio's kinematic prediction (`prediction` in `slice_info.config`): heat waits and calibration count 0 s, and nothing scales the result per printer. shambu labelled them "estimate" in the report, but the hand-back template said "<time> h", and nothing logged the real duration.

## 3. What the tool knew and did not use

- The mesh: `mesh.py` already found downward faces and could draw an outline. The support margin used the bounding box.
- The slice: `build --slice` runs Studio, whose exit -101 names a real clash. The checker refused to export before Studio could answer.
- The printer: `printer watch` streamed percent, remaining minutes and layer, but stored no timestamp and never set them against the estimate.

## 4. Required behaviour

1. Margin: an estimate per part from the measured spreads, the larger of two facing spreads (not the sum) plus a line allowance, a measurement on request, and an estimate short of the margin as a WARN that the slice decides.
2. Placement: footprints from the mesh outline; a packer that fits the five parts on one plate; a give-up prints one `PLACEMENT NOT DONE` line and no origin FAILs; each line names its kind.
3. The slice is the final test, and a -101 names the two objects.
4. Versions: `doctor --expect-version` and one pinned install (`@v<version>`). The skill stops on a mismatch.
5. Verbs: replace a mesh, add a filament, move an object, place the tower, re-slice a saved project, and a logged escape hatch that runs only with the user's yes.
6. `sweep --write-spec` writes the winning variant.
7. Every export re-slices.
8. Skill: the hand-back is a gate; time is "Studio estimate"; a cause is stated only when an output line shows it, else "The check said X. The slicer said Y. The cause is not established."
9. Estimates: log every poll with its own timestamp and the speed level, tie the log to the printed `.gcode.3mf`, and show a calibrated time only from several agreeing prints.

## 5. Acceptance (what 0.4.0 does)

- The five post-mortem part sizes fit on one plate: `BottomLeftPacker` places them with the estimated and with the measured spreads (unit tests; reference 08 section 11.5).
- Three parts 10 mm apart with tree supports give no `FAIL body overlap`, bed or reach line; a short estimated spread is a `WARN margin rule` line ("the slice decides"), not a FAIL.
- A part that fits nowhere gives one `PLACEMENT NOT DONE` line with the arithmetic, no line with a negative coordinate, and no export even with `--keep-going`.
- Two parts whose bodies meet: `FAIL body overlap` with the overlap size. A `brim_margin` or `support_spread` in the spec replaces the estimate; `--probe-supports` measures it with a cached solo slice.
- A Studio -101 after slicing names the two spec objects; a -21 says Studio's arrange could not fit the parts (24 mm between objects with tree supports).
- `doctor --expect-version 9.9.9` exits 3, prints `MISMATCH` and a `Fix:` line with the tagged install, and puts the `shambu` section first in `--json`. doctor has `--printer-timeout` and needs no `timeout` wrapper.
- Two filaments: `wipe_tower` `"auto"` sizes the tower from the tallest part (41.39 mm square plus an 8 mm brim at 218 mm with the rib wall), searches a spot both nozzles reach, tries the rib wall off at 35, 30, 25, 22 and 20 mm only when Studio's size does not fit, and FAILs naming the blocker. A process-level `support_interface_filament` counts as a second filament.
- Every unsliced export carries `Standard#1` nozzle stats; verify refuses one with `[]` or `#0`, and `patch --repair-nozzle-stats` fixes an older file.
- `patch --replace-mesh`, `--add-filament` (a preset with no colour takes the next unused default colour, with a WARN naming it), `--move`, `--tower` and `--dry-run` edit a project and report the pair rule; `shambu slice FILE [--no-check]` re-slices it, adding the unchecked pass per plate only after a -101 or a warning -100 and listing what it skipped; `shambu studio -- ARGS` logs the command, the exit and the log.
- `sweep --write-spec OUT.json [--pick NAME]` writes a spec that `build` accepts, with a `_sweep` provenance key.
- The report says "Studio estimate H h M min"; a measured range or a calibrated time appears only from `shambu calibrate` data, a calibrated time only with three qualifying prints. `printer watch --log FILE --gcode3mf PROJECT [--wait-for-start]` logs no IP and no serial.
- The eval cases 4 to 11 in `skills/shambu/evals/evals.json` describe the skill's side.

## 6. Open questions

- How far tree supports really spread on tall parts, measured on more than two parts; whether the spread fades with z for an overhanging neighbour.
- Why D.7 ran at 0.75 x Studio and E7a at 1.05 x. A non-Standard speed level on D.7 is the most likely cause and was not logged; the watch log now records it.
- The packer does not find the nested layout: the five plate-14 parts without `at` pack on two plates even with measured spreads; the printed one-plate layout needs `at` for the feeler and the cradle (the cradle in the feeler's pocket); Studio's own arrange exits -21 on the same parts. A documented limit of 0.4.0.
- Studio's conflict check is not deterministic at a 0.25 mm gap (2 of 4 identical checked slices exited 0), and one of four identical slices of the front-tower layout crashed with -139 in tree support and passed on rerun.

## 7. Interim rule (before 0.4.0 is installed)

- A margin-rule FAIL is not a plate count. Tell the owner the rule and its numbers, set `brim_margin` on the part (it replaces both margins), and slice.
- Say "Studio estimate" for every time.
- Run `shambu --version` and compare it with the plugin version before a session.

## 8. Evidence

- Post-mortem: `content/drafts/openarm/g2-redesign/55-shambu-post-mortem.md` in the owner's blog repo.
- Trial specs and reports: `printing-tasks/plates/plate-14-housing-camera/` in the same repo (plate-14-B1, -B2, -C, plate-14b, plate-15, plate-14-all-on-one-E7a-pla-interface).
- Timing: `printing-tasks/plates/plate-9-bracket/bambu/print-watch-D7.jsonl` and the M73 lines in the printed `.gcode.3mf`; the E7a watch of 2026-10-21.
- Source, before the fix: `src/shambu/layout.py:97-104` (support margin), `:232-237` (pair test), `:130-180` (packer), `src/shambu/build.py:519-529` (give-up path), `:686-690` (`brim_margin`), `:695-696` (`at`), `:890-893` (the fixed tower).
- References: `skills/shambu/references/00-findings.md` section 10 (every 0.4.0 rule with its Studio source line), 08 section 11 (layout), 06 sections 10 and 11 (tower, time), 03 section 8 and "Project edits", 04 Track 7 (watch log).

## 9. Resolution (shambu 0.4.0)

- Layout (section 4, items 1 to 3): `layout.support_spread` (tree `min(22, max(8, 0.10 x height))`, normal 5 mm), `layout.pair_check` (the larger spread plus the 1 mm line allowance), `mesh.silhouette` footprints, `BottomLeftPacker`, `probe.py`, and report lines that name their kind (`FAIL body overlap`, `FAIL margin rule`, `WARN margin rule`, `WARN brim`, `PLACEMENT NOT DONE`).
- Versions: `doctor --expect-version`, the `shambu` section, the tagged install in `/shambu:setup`, and a test that keeps every version literal in the skills and the README equal to `__version__`.
- Verbs: `edits.py` (`--replace-mesh`, `--add-filament`, `--move`, `--tower`), `shambu slice`, `shambu studio`, `tower.py` with the `wipe_tower` spec key, `RepairNozzleStats`.
- Time: `printer watch --log` with `--gcode3mf`, `shambu calibrate`, and the "Studio estimate" wording in the report.
- Skill: the hand-back is a gate with Pre-flight, Slice and Cause lines; the cause rule; no plate count from `check`; never unzip a 3MF or run Studio by hand.
