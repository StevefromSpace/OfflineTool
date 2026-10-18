# Plan and roadmap

This document defines milestone boundaries and acceptance criteria. Only Milestone 1 is
implemented; everything after it is a *potential* extension, to be scheduled only when a
real client requirement exists.

## Milestone 1 — Reliable DOCX → PDF conversion (implemented)

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
   as soon as that document is done; failures to delete are logged (without paths).

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
- [ ] CI workflow (`.github/workflows/ci.yml`) has **not yet run on GitHub**. Its Python
      3.11 / 3.13 / 3.14 matrix has only been exercised locally with 3.14.

Verified locally on Windows 11 with Python 3.14.5, PySide6(-Essentials) 6.12.0 and
LibreOffice 26.2.1. Reviewed twice by independent security, QA and privacy reviewers
(Stage One and a re-verification round); their Critical/High/Medium findings were fixed or,
where a finding did not hold up under testing, documented, before the first commit.

### Known limitations (Milestone 1)

- Cancel takes effect after the current document finishes (or times out).
- Rendering fidelity is LibreOffice's; fonts missing on the machine are substituted.
- Linked (not embedded) images are deliberately left out of the PDF; the UI does not yet
  say when this happened.
- No input size limit beyond the per-document timeout.
- The file dialogs are the standard Windows ones, which remember recently used folders.
- LibreOffice 26.2.1 crashes (exit 0xC0000409) when its profile path is too deep: measured to
  work with a temp folder path up to 120 characters, fail from ~125. Profile folder names are
  kept short for headroom, and the crash is reported with a hint to use a shorter `TEMP`.
- Only linked *images* are verified blocked; other external references (OLE links,
  `INCLUDETEXT`, remote templates) are untested. UNC image links caused no SMB connection
  in a one-off manual check.
- Password-protected `.docx` files are rejected (they are not ZIP containers).
- Not yet packaged as a standalone installer; runs from a Python environment.

## Potential future extensions (not scheduled)

Each would be a separate milestone with its own acceptance criteria, added only on demand:

- Additional input formats: `.doc`, `.odt`, `.rtf`, Excel, PowerPoint (likely reusing the
  LibreOffice adapter with a format table).
- PDF operations: merge, split, reorder, rotate, compress.
- Image ↔ PDF conversion.
- OCR of scanned PDFs (would require a local OCR engine; no cloud).
- PDF export options (PDF/A, image quality).
- Collision policy choice (rename / skip / ask).
- Standalone Windows installer (e.g. PyInstaller) with documented offline verification.
- Tell the user when linked images were left out of a PDF.
- Optional input size limit.
- Drag-and-drop file selection.
