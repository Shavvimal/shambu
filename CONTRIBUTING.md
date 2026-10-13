# Contributing

Thanks for helping. shambu is small on purpose: one CLI, one Python package, two skills.

## Set up

```sh
git clone https://github.com/Shavvimal/shambu.git && cd shambu
make install          # uv sync --group dev
make check            # ruff, mypy, pytest
```

Python 3.11 or newer and [uv](https://docs.astral.sh/uv/). The tests run offline and never call the Bambu Studio
binary or a printer; integration tests skip unless `SHAMBU_INTEGRATION=1` and a Bambu Studio install is present.

## Where things live

| Change | Where | Read first |
|---|---|---|
| A new print recipe | `src/shambu/recipes.py` | `skills/shambu/references/07-print-recipes.md` |
| A new 3MF patch | `src/shambu/patches.py` + `threemf.py` | `references/03-3mf-format.md` |
| A new limit rule or placer | `src/shambu/layout.py` | `references/01-source-cli.md` section 2 and 8 |
| CLI flag handling | `src/shambu/studio.py` (`CliCommand`) | `references/01-source-cli.md` section 1 |
| Printer fields | `src/shambu/printer.py` | `references/04-app-printer-control.md` |
| Skill text | `skills/shambu/SKILL.md`, `skills/setup/SKILL.md` | the plugin docs |

Every rule the code encodes about Bambu Studio should cite the reference that proved it. The
review rubric in `REVIEW-RUBRIC.md` is what a pull request is judged against.

## Pull requests

- One topic per PR, squash-merged. Add or change a test for every behaviour change.
- Bump `__version__` in `src/shambu/__init__.py` and `version` in `.claude-plugin/plugin.json` together when
  anything under `src/`, `skills/` or `.claude-plugin/` changes (CI enforces the bump; a test enforces the match).
- Add a line under `## [Unreleased]` in `CHANGELOG.md`.
- Never commit a printer serial, access code, IP address or personal path. Run
  `rg -n '192\.168|access_code|/Users/'` before pushing.

## Printer safety

The printer code is read-only by design: subscribe plus one `pushall`. Pull requests that add print start, pause,
stop, upload or any other printer command will not be merged.
