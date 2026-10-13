## What

<!-- One paragraph: the behaviour before, the behaviour after. -->

## Why

<!-- The print problem or defect. Link the issue. Cite the reference section or Bambu Studio source line that justifies a new rule. -->

## Checklist

- [ ] `make check` passes (ruff, mypy, pytest).
- [ ] Tests added or changed for the behaviour.
- [ ] `__version__` in `src/shambu/__init__.py` and `version` in `.claude-plugin/plugin.json` bumped together if anything under `src/`, `skills/` or `.claude-plugin/` changed.
- [ ] `CHANGELOG.md` updated under `[Unreleased]`.
- [ ] No printer serial, access code, IP address or personal path in the diff.
- [ ] Printer code stays read-only (subscribe + `pushall` only).
