# CLAUDE.md — project instructions

Offline, privacy-focused desktop app (PySide6) that converts `.docx` to PDF with a locally
installed LibreOffice. Windows-first. See `docs/PLAN.md` for milestones and scope.

**Status: development paused at the v0.1 checkpoint (2026-10-20).** See `docs/STATUS.md`. No new
features; do not expand into a full document/PDF platform.

## Commands (Windows, from repo root)

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
.venv\Scripts\python -m pytest                      # all tests (integration runs if LibreOffice is installed)
.venv\Scripts\python -m pytest -m "not integration" # unit tests only
.venv\Scripts\ruff check .
.venv\Scripts\ruff format --check .
.venv\Scripts\python -m offline_doc_tools           # run the app
```

## Layout

- `src/offline_doc_tools/engine.py` — `DocumentConverter` protocol + `ConversionError` hierarchy
  (messages are shown to users; write them in plain language).
- `libreoffice.py` — discovery (`find_soffice`), `LibreOfficeConverter` (hardened throw-away
  profiles, output redaction), `run_process` (timeout + process-tree kill, output via temp
  files). The only module that knows about LibreOffice.
- `service.py` — validation, temp workspace, PDF verification, collision-safe placement,
  per-file results. Engine-agnostic. No Qt imports.
- `worker.py` — `QObject` run on a `QThread`; wraps the service and emits signals.
- `main_window.py` — UI only. No conversion logic.
- `app.py` — logging setup and entry point.
- `updater/` — click-started self-update (no Qt imports): `manifest` + `ed25519` (signed
  manifest), `github` (private releases), `package` (safe staging), `installer` + `helper`
  (replacement with rollback, run by the staged new exe via `--apply-update`), `service`,
  `runtime`. Run from `worker.BackgroundTask`, shown by `main_window.py`. See `docs/UPDATES.md`.

## Non-negotiable rules

- No network access at runtime: no telemetry, downloads, cloud APIs. The one exception, requested
  by the owner on 2026-10-19, is the click-started updater in `updater/` (private GitHub
  releases, signed manifest, installs only after user confirmation, sends nothing about the user
  or documents). No automatic or background checks, no unsigned or unverified installs, no token
  or key in source; see `docs/PLAN.md` decision 11.
- Keep `BlockUntrustedRefererLinks` in `_PROFILE_SETTINGS`: without it LibreOffice fetches
  images linked by URL and embeds linked local files. Guarded by
  `test_linked_images_are_neither_fetched_nor_embedded`; re-run it after LibreOffice upgrades.
- Keep LibreOffice profile paths short (`odt-profile-*/p<n>`): LibreOffice 26.2.1 crashes
  (0xC0000409) once the profile path gets too deep.
- GUI tests rely on the autouse `_dispose_qt_widgets` fixture in `tests/conftest.py`; without
  it, leftover windows were finalised by the cyclic GC mid-test and aborted the interpreter.
- Never modify, move or delete source documents. The engine only ever sees a private copy.
- Never overwrite an existing output file; `place_output` uses exclusive create.
- Subprocesses: argument lists only, never `shell=True`; always a timeout.
- Logs must not contain document contents, user file names, or unredacted temp paths (they
  contain the Windows user name). Only unexpected-error tracebacks are exempt.
- Depend on `PySide6-Essentials`, not the full `PySide6` metapackage (avoids WebEngine).
- Never commit real documents. Tests generate synthetic `.docx` files (`tests/conftest.py`).
- Don't add a database, plugin system or DI framework without a concrete need.
- Don't build features outside the current milestone; record them in `docs/PLAN.md`.
- No licence file until the owner decides on one. Don't invent a product name.

## Testing conventions

- Unit tests mock the subprocess via the `runner` argument of `LibreOfficeConverter`, or use
  `FakeConverter` from `tests/test_service.py`. They must pass without LibreOffice.
- Qt tests run headless (`QT_QPA_PLATFORM=offscreen`, set in `conftest.py`) and must not open
  modal dialogs.
- `tests/integration/` uses the real LibreOffice and skips when it is not found.
- Write the failing test first; keep `ruff check` and `ruff format --check` clean.
- Dev tools are pinned in `pyproject.toml`; bump them deliberately.

## Review agents

`.claude/agents/` defines read-only `security-reviewer`, `qa-reviewer` and
`privacy-reviewer` subagents. Use them for independent reviews before publishing changes.
