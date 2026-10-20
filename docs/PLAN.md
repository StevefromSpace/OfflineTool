# Plan and roadmap

> **Status (2026-10-20): development is paused at the v0.1 checkpoint.** Nothing below is scheduled.
> Do not expand OfflineTool into a competing full document/PDF platform; Stirling PDF already covers
> that direction more broadly. See `docs/STATUS.md`.

This document defines milestone boundaries and acceptance criteria. Milestone 1 is done.
Milestone 2's Windows bundle was verified locally on 2026-10-19 (see its verification record);
two criteria stay open (CI for the Milestone 2 commits, which do not exist yet, and a fresh
independent review of the final tree). Everything after Milestone 2 is a *potential* extension,
to be scheduled only when a real client requirement exists.

## Milestone 1 — Reliable DOCX → PDF conversion (done)

### Scope

Windows-first desktop app (PySide6) that converts one or more `.docx` files to PDF using a
locally installed LibreOffice running headless.

### Architecture

| Layer | Module | Responsibility |
|---|---|---|
| UI | `offline_doc_tools.main_window` | File/folder pickers, progress, results table, summary, "open output folder". No conversion logic. |
| Background worker | `offline_doc_tools.worker` | Runs a batch on a `QThread`; relays progress via Qt signals; supports "cancel after current file". |
| Conversion service | `offline_doc_tools.service` | Validates inputs, creates an isolated temporary workspace, calls the engine, verifies the PDF, places it in the output folder without overwriting, records per-file results. |
| Engine interface | `offline_doc_tools.engine` | `DocumentConverter` protocol and the `ConversionError` hierarchy. The only contract the service depends on. |
| LibreOffice adapter | `offline_doc_tools.libreoffice` | Locates `soffice`, builds the argument list, runs it with a timeout, kills the process tree on timeout. |
| App entry | `offline_doc_tools.app` | Local log setup and `QApplication` start-up. |

### Key design decisions

1. **Copy-then-convert.** Each source is copied into a private temp directory under an ASCII
   name (`source.docx`) before LibreOffice sees it. This guarantees the original is never
   opened for writing, keeps LibreOffice lock files (`.~lock.*#`) out of the user's folders,
   sidesteps Unicode/space quoting issues on the LibreOffice command line, and keeps user
   file names out of LibreOffice's diagnostic output (and therefore out of our log).
2. **Isolated, throw-away, hardened LibreOffice profile** (`-env:UserInstallation=file:///…`
   inside the temp directory). Avoids lock conflicts with a LibreOffice the user already has
   open, and nothing (e.g. recent-documents list) persists. Before first use the profile is
   seeded with `BlockUntrustedRefererLinks=true`: without it LibreOffice 26.2.1 fetched images
   linked by URL (a network request) and embedded linked local files into the PDF (verified
   with a loopback server, see the integration tests). After a timeout kill the profile may be
   half-written, so a fresh one is used for the rest of the batch.
3. **Never overwrite.** The finished PDF is written to the output folder with exclusive
   create (`open(..., "xb")`). If `report.pdf` exists the result is `report (1).pdf`,
   `report (2).pdf`, … The UI reports when a file was renamed. *Assumption: automatic
   renaming is preferable to prompting per file; this can be revisited.*
4. **Do not trust the exit code alone.** LibreOffice can exit 0 without producing output.
   A conversion only succeeds if the expected PDF exists, is non-empty and starts with
   `%PDF-`.
5. **Sequential conversion.** One LibreOffice process at a time. Simpler and avoids
   profile contention; batch throughput is not a Milestone 1 goal.
6. **Timeout with process-tree kill.** Default 120 s per document. On Windows `soffice.exe`
   launches `soffice.bin`, so the whole tree is terminated (`taskkill /T /F`), not just the
   launcher. Process output goes to temporary files, not pipes, so a lingering child process
   holding the handles can never block the worker; only the last 64 KiB is read.
7. **Discovery order:** `OFFLINE_DOC_TOOLS_SOFFICE` env var → standard install locations
   (Program Files on Windows) → `PATH`. On Windows a `PATH` hit must be an absolute `.exe`/
   `.com` outside the current directory (Windows searches the current directory first and
   runs `.bat`/`.cmd` through `cmd.exe`). Elsewhere `PATH` comes before `/Applications`.
   No registry lookup and no automatic install.
8. **Privacy-preserving logs.** A small rotating log in the user's local app-data folder
   records job numbers, outcomes, error categories, exit codes and, on failure, LibreOffice's
   output (last 2,000 characters), with temporary paths redacted first (they contain the
   Windows user name). No document contents
   or user file names. Unexpected-error tracebacks are logged in full and could contain a
   path; the README discloses this.
9. **Short-lived temporary copies.** Each document's private copy and staged PDF are deleted
   as soon as that document is done; failures to delete are logged (without paths). The batch
   workspace (`offline-doc-tools-*`) and the LibreOffice profile folder (`odt-profile-*`) are
   removed when the batch ends. If the app is killed or a file is locked, a folder of either
   name can remain in the system temporary folder and be deleted manually (the README says so).

### Acceptance criteria

- [x] Select one or more `.docx` files; choose an output folder.
- [x] Conversion runs off the GUI thread; the window stays responsive.
- [x] Progress, per-file success/failure, and a final summary are shown.
- [x] Missing LibreOffice is detected at start-up and before converting, with install guidance.
- [x] Invalid inputs (missing, not a file, wrong extension, empty, not a DOCX/ZIP container,
      Word lock file) are rejected with a clear message and do not stop the batch.
- [x] Timeouts, non-zero exits, and missing/invalid output are reported per file.
- [x] Existing output files are never overwritten.
- [x] Source documents are never modified, moved or deleted.
- [x] File names with spaces and non-ASCII characters work.
- [x] "Open output folder" opens the folder locally.
- [x] Images linked by URL or local path are neither fetched nor embedded.
- [x] Unit tests run without LibreOffice; integration tests run when it is installed.
- [x] Ruff lint and format checks pass.
- [x] CI workflow (`.github/workflows/ci.yml`) passes on GitHub: run 39093737555
      (https://github.com/StevefromSpace/OfflineTool/actions/runs/39093737555) on commit
      `b6d4549` completed successfully. It ran lint and the unit tests on `windows-latest` with
      Python 3.11, 3.13 and 3.14. The integration tests were skipped there because LibreOffice
      is not installed on the runner.

Verified locally on Windows 11 with Python 3.14.5, PySide6(-Essentials) 6.12.0 and
LibreOffice 26.2.1; the integration tests (real LibreOffice) have been run locally only.
Reviewed twice by independent security, QA and privacy reviewers
(Stage One and a re-verification round); their Critical/High/Medium findings were fixed or,
where a finding did not hold up under testing, documented, before the first commit.

### Known limitations (Milestone 1)

- Cancel takes effect after the current document finishes (or times out).
- Rendering fidelity is LibreOffice's; fonts missing on the machine are substituted.
- Linked (not embedded) images are deliberately left out of the PDF; the UI did not say when
  this happened (Milestone 2 adds a notice).
- No input size limit beyond the per-document timeout.
- The file dialogs are the standard Windows ones, which remember recently used folders.
- LibreOffice 26.2.1 crashes (exit 0xC0000409) when its profile path is too deep: measured to
  work with a temp folder path up to 120 characters, fail from ~125. Profile folder names are
  kept short for headroom, and the crash is reported with a hint to use a shorter `TEMP`.
- Only linked *images* are verified blocked; other external references (OLE links,
  `INCLUDETEXT`, remote templates) are untested. A UNC image link was observed to cause no
  SMB connection in a one-off manual check; only the log of the positive control (the path
  opened directly) was saved, not the logs of the conversion runs, so this is an observation,
  not recorded evidence.
- Password-protected `.docx` files are rejected (they are not ZIP containers).
- Not packaged: runs from a Python environment (Milestone 2 adds a locally built, unsigned
  bundle; there is still no installer).

## Milestone 2 — Windows distribution and robustness (verified locally; CI and re-review open)

Status: the Windows bundle and the robustness items were checked on 2026-10-19 (verification
record below). A box is ticked only after its criterion has been checked. Two boxes stay open.

### Scope

Let the app run on Windows without a Python environment, in a form that can be checked
automatically, and close the reliability, logging and documentation gaps left after Milestone 1.

In scope:

- **Windows bundle.** A repeatable local build of an unsigned, windowed, folder-style ("onedir")
  bundle that includes the licence texts of its third-party components.
- **Bundle smoke test.** A script that starts a copy of the built bundle in a sanitised
  environment and checks it.
- **Linked-image notice.** After a conversion, the results table tells the user how many images
  in a document were left out because they are only linked and are never fetched.
- **Reliability.** A missing, unwritable or full temporary folder produces a clear error that
  mentions the temporary folder. Messages and logs contain no paths, and logs contain no user
  file names, not even extensions.
- **Documentation corrections** from the Milestone 1 final audit: the README says that only
  images stored inside the `.docx` appear in the PDF and that other external references are
  untested; the UNC observation is labelled a one-off manual observation and states which
  evidence was saved; the leftover-folder note names `odt-profile-*` as well as
  `offline-doc-tools-*`; the CI status is stated precisely.

Out of scope: an installer, code signing and a public release (recorded under *Potential
future extensions*), bundling LibreOffice, an automatic updater (never: the app must not
download or install anything), non-Windows bundles, and choosing a licence for the application
(an owner decision). The owner-requested exception, added on 2026-10-19 after the original
"never": a click-started, consent-based **updater** (decision 11).

### Key design decisions

1. **Isolated, pinned, committed build.** `python scripts\packaging\build_windows.py` (Python
   3.11 or newer, on Windows) creates a virtual environment in `build\packaging-venv\`,
   installs exact pins from the `build` extra in `pyproject.toml` and runs PyInstaller with the
   committed spec `scripts\packaging\offline_doc_tools.spec`. The bundle's packages come from
   the pins, not from whatever is installed on the developer's machine, and both the pins and
   the spec are reviewed like code. Pinned: PyInstaller 6.22.2, pyinstaller-hooks-contrib 2026.6
   and PySide6-Essentials 6.12.0 (the `build` extra in `pyproject.toml`).
2. **Folder output, windowed.** `dist\OfflineDocTools\OfflineDocTools.exe` (no console window)
   plus `dist\OfflineDocTools\licenses\`. A folder can be inspected file by file, which the
   smoke test's file checks rely on.
3. **Minimal Qt.** Of Qt's modules only QtCore, QtGui and QtWidgets are bundled (with the
   PySide6/shiboken libraries and Qt plugins they need), matching the app's
   `PySide6-Essentials` dependency. No Qt network module and no WebEngine.
4. **LibreOffice stays a separate prerequisite.** It is neither bundled nor downloaded. The
   bundle finds it as the source version does (Milestone 1, decision 7). Only LibreOffice
   26.2.1 has been verified.
5. **No installer, no code signing, no public release.** Expect SmartScreen and Defender
   prompts for an unsigned executable. The application itself has no licence yet, which
   blocks public distribution. The bundle carries the licence texts of its third-party
   components.
6. **Network.** The only network use in the build-and-check workflow is pip downloading the
   pinned build dependencies at build time. The bundle itself opens no network endpoints on
   its own; its only runtime network use is the click-started updater (decision 11).
7. **Smoke test on a copy, in a sanitised environment.**
   `python scripts\packaging_smoke\smoke_test.py [--app-dir DIR]` launches a copy of the bundle
   offscreen with an environment that contains no Python or virtual environment, so the build
   machine's Python cannot make a broken bundle appear to work. It checks the start-up log
   line, that the process stays alive, that no network endpoints are opened, that forbidden
   files (Qt network and WebEngine files, documents) are absent and that the licences are
   present. A pass means one short start-up looked right; it is not a security audit and not a
   clean-machine test.
8. **Linked-image notice from verified structures only.** After a conversion the results table
   says how many images in the document were left out because they are only linked (by web
   address or file path) and are never fetched. Only document structures verified with the
   real LibreOffice are detected; others are not counted. The notice is therefore not a
   guarantee: a missing notice does not prove that nothing was left out, and no document may
   claim that detection is complete. Detected structure (the only one): a picture (`a:blip`) whose
   `r:link` points to an external image relationship, in any part of the document (inline pictures,
   headers, linked shape fills). Other external relationships that the sanitiser removes (for
   example an attached template) are counted in the log only, with no notice in the results table.
9. **Temporary-folder failures are reported clearly.** A missing, unwritable or full
   temporary folder gives an error that mentions the temporary folder. The message and the log
   contain no paths (a path in the temporary folder contains the Windows user name).
10. **No user file names in logs, not even extensions.** Unexpected-error tracebacks remain
    the documented exception (Milestone 1, decision 8).
11. **Updater: private release channel, signed, click and consent (owner request,
    2026-10-19; extended the same day from a notification-only check).** This reverses the earlier rule
    that the app never uses the network at runtime, for one feature. Releases live in a separate
    PRIVATE GitHub repository, read with a user-entered read-only token (DPAPI-protected). A signed
    Ed25519 manifest (key built into the application) names the version, package file, SHA-256, size
    and helper protocol; GitHub's own fields and every URL in its replies are ignored; the one
    redirect of an asset download is followed only to `*.githubusercontent.com`, without the token.
    The package is verified, extracted to a staging folder next to the installation and installed by
    the new package's own executable run as a helper, which keeps a backup and rolls back if the new
    version does not report a successful start. Never automatic or background; installing needs the
    user's confirmation. The bundle carries Python's `ssl` and OpenSSL 3 (Apache-2.0 text shipped);
    `bundle_policy.py` requires them and still rejects Qt Network, WebEngine and `_hashlib`. The smoke
    test still expects **no** network endpoint at start-up. Dormant until `config.py` holds the
    repository and public key. Full design, checks and owner set-up: `docs/UPDATES.md`. Verified with
    fakes and a real packaged update/rollback run; not verified against a real private repository or
    the production key. Out of scope: automatic or background checks, silent installation, delta
    updates, GitHub App device flow.
12. **Licence notice in the window; update controls hidden when not configured (v0.1 delivery,
    2026-10-19).** The window has an *About and licences…* button that says Qt and PySide6 are used
    under the LGPL-3.0 as separate library files and where the notices are (the notice file beside
    the executable was the only place before). The update controls are hidden unless a repository
    and a trusted key are configured in `updater/config.py`, so a client of the v0.1 build is not
    offered a feature that cannot work. Both are covered by `tests/test_about_notice.py` and
    `tests/test_update_ui.py`.
13. **Qt and PySide6 source travels with the delivery (proposed 2026-10-19; the owner confirms).**
    The unmodified 6.12.0 source archives, with checksums and a plain description, are supplied as a
    separate folder next to the client ZIP (`dist\release\OfflineDocTools-0.1.0-corresponding-source\`)
    instead of a written offer, which would need a named contact and a multi-year commitment. Whether
    the PyPI binaries were built from exactly those archives is not verified. Not legal advice; see
    `docs/DISTRIBUTION.md`.

### Acceptance criteria

Unchecked on purpose: a box is ticked only after the criterion has been verified.

Build and bundle

- [x] `python scripts\packaging\build_windows.py` builds the bundle on Windows with Python 3.11
      or newer.
- [x] The build uses an isolated virtual environment at `build\packaging-venv\`, the exact pins
      of the `build` extra in `pyproject.toml` and the committed spec
      `scripts\packaging\offline_doc_tools.spec`.
- [x] The output is `dist\OfflineDocTools\OfflineDocTools.exe` (windowed) plus
      `dist\OfflineDocTools\licenses\`.
- [x] Of Qt's modules only QtCore, QtGui and QtWidgets are bundled: no Qt network module and no
      WebEngine files.
- [x] LibreOffice is not bundled and stays a separate prerequisite.
- [ ] The only network use of the build is pip downloading the pinned build dependencies.
- [x] There is no installer, no code signing and no public release, and the README says that
      SmartScreen/Defender prompts are expected and that the missing application licence
      blocks public distribution.

Smoke test (`python scripts\packaging_smoke\smoke_test.py [--app-dir DIR]`)

- [x] It launches a copy of the built bundle offscreen with a sanitised environment (no Python
      or virtual environment) and passes.
- [x] It checks that the start-up log line is present.
- [x] It checks that the process stays alive.
- [x] It checks that no network endpoints are opened.
- [x] It checks that forbidden files (Qt network and WebEngine files, documents) are absent.
- [x] It checks that the licences are present.
- [x] Manual check, outside the smoke test: the bundle, run on a machine with LibreOffice
      26.2.1 installed, converts a synthetic `.docx` to a valid PDF.

Linked-image notice

- [x] After a conversion, the results table says how many images in the document were left out
      because they are only linked (by web address or file path).
- [x] Every detected structure is verified with the real LibreOffice (integration test).
- [x] The final list of detected structures is stated in the README and in this plan, and
      neither claims that detection is complete.

Reliability

- [x] A missing temporary folder gives a clear error that mentions the temporary folder.
- [x] An unwritable temporary folder gives a clear error that mentions the temporary folder.
- [x] A full temporary folder gives a clear error that mentions the temporary folder.
- [x] The messages and log entries for these three cases contain no paths.
- [x] Logs contain no user file names, not even extensions.

Documentation and quality

- [x] The README says that only images stored inside the `.docx` appear in the PDF and that
      other external references are untested.
- [x] The UNC observation is labelled a one-off manual observation that names the evidence
      saved (the positive-control log only), in the README and in this plan.
- [x] The leftover-folder note names both `offline-doc-tools-*` and `odt-profile-*`.
- [x] The CI status in the README and in this plan matches the facts (Milestone 1: run
      39093737555 on `b6d4549`, lint and unit tests only).
- [x] Unit tests pass without LibreOffice, the integration tests pass with LibreOffice 26.2.1,
      and `ruff check .` and `ruff format --check .` are clean.
- [ ] CI passes for the Milestone 2 commit(s): lint and the unit tests on Python 3.11, 3.13 and
      3.14. Open: the Milestone 2 changes are not committed or pushed, so no CI run exists.
- [ ] Independent security, QA and privacy reviews of the Milestone 2 changes: findings fixed
      or documented. Open for the final tree: the updater and sanitiser were reviewed earlier
      (findings are in `docs/UPDATES.md`), but the About button, the hidden update controls and
      the final bundle have not been reviewed independently.
- [x] No placeholder comment for a result that was not yet known is left in `README.md` or in
      this plan, and every claim in them matches the verified build.

### Verification record

Date: 2026-10-19. Machine: Windows 11 Home build 26300, 7.5 GB RAM. Python 3.14.5, PyInstaller
6.22.2, PySide6-Essentials and shiboken6 6.12.0, LibreOffice 26.2.1.2; the machine has the Visual C++
14.51 Redistributable. Built with `build_windows.py --vc-runtime-prerequisite`.

| What | Artifact exercised | Result |
|---|---|---|
| `ruff check .`, `ruff format --check .` | source tree | clean (66 files) |
| Unit tests `-m "not integration"`, with `openssl` available | source tree | 991 passed, 0 failed, 0 skipped. Without `openssl` on `PATH`, 14 of them (the release tool and the Ed25519 cross-check) are skipped; that was the state of the baseline run before the About-button change (971 passed, 14 skipped) |
| Integration tests with the real LibreOffice | source tree | 6 passed |
| Bundle build | `dist\OfflineDocTools\`: 37 files, 60,345,265 bytes, no Microsoft runtime DLLs | accepted by `bundle_policy` |
| Smoke test (8 checks) | the bundle, and the extracted final ZIP (39 files) | passed, both |
| Conversion by the windowed app (file dialog, output folder, *Convert to PDF*), driven by script | a copy of the bundle, and the extracted final ZIP | `Finished: 1 converted, 0 failed.`; valid 22,786-byte PDF; source unchanged; log has no file name or path |
| Packaged update test `update_e2e.py` (OLD = final bundle; NEW and BROKEN built from a scratch copy of the final source, NEW with a tagged helper) | real executables, local stand-in for GitHub, throw-away test key | 16 checks passed: install by the new package's own helper, exact file-by-file match, obsolete file removed, no leftovers, BROKEN rolled back to an exact copy |
| Manual test by the owner | the build the owner launched (not recorded which) | owner reported: the app opened and the tested workflow worked |

Final client ZIP: `dist\release\OfflineDocTools-0.1.0-win64-client-final.zip` (copy in `dist\verified\`),
25,520,615 bytes, SHA-256 `f41f3f705d2bc221e7949464fc7ceb3fd26d2ffc9823b8dd19872769b773f463`; its
`OfflineDocTools.exe` is 2,387,819 bytes, SHA-256
`3fc99bfbc9e797106c90ee1cd4f0c45ad7005fc0b8c22ecae779f4a65b4cb541`.

Not verified: CI for these changes; a clean machine and a PC without the Redistributable;
SmartScreen/Defender behaviour; the build's pip download; the updater against a real private
repository, token or production key; an independent review of the final tree.

### Known limitations (Milestone 2)

- The executable is unsigned: expect SmartScreen and Defender prompts. There is no installer;
  the whole `dist\OfflineDocTools\` folder must be kept together.
- The bundle is Windows-only, needs a separately installed LibreOffice and is verified only
  with LibreOffice 26.2.1.
- The smoke test checks one short start-up on the machine that built the bundle. It is not a
  clean-machine test, it does not convert a document, and it cannot show that no network
  connection is ever made.
- The linked-image notice covers only the structures verified with the real LibreOffice. Its
  absence proves nothing, and other external references (linked OLE objects, `INCLUDETEXT`
  fields, remote templates) remain untested.
- The bundle is not distributed publicly, and the application has no licence yet.
  Third-party notices, the open LGPL and Microsoft-runtime questions and the prepared corresponding
  source are recorded in `docs/DISTRIBUTION.md`; proposed client terms are only a draft in
  an unapproved draft kept outside this repository.

## Potential future extensions (not scheduled)

Each would be a separate milestone with its own acceptance criteria, added only on demand:

- Additional input formats: `.doc`, `.odt`, `.rtf`, Excel, PowerPoint (likely reusing the
  LibreOffice adapter with a format table).
- PDF operations: merge, split, reorder, rotate, compress.
- Image ↔ PDF conversion.
- OCR of scanned PDFs (would require a local OCR engine; no cloud).
- PDF export options (PDF/A, image quality).
- Collision policy choice (rename / skip / ask).
- Windows installer (the Milestone 2 bundle is a plain folder), code signing and a public
  release, with documented offline verification. A public release is blocked until the owner
  chooses a licence for the application.
- Detect more kinds of linked content (further linked-image structures, linked OLE objects,
  `INCLUDETEXT` fields, remote templates). Each needs verification with the real LibreOffice
  first.
- Optional input size limit.
- Drag-and-drop file selection.
