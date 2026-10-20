# Offline Doc Tools

*(Working title. Not a final product name.)*

A privacy-focused desktop application that converts Microsoft Word documents (`.docx`) to PDF
**entirely on your own computer**, using a locally installed copy of LibreOffice. Documents are
never uploaded anywhere.

> **Status:** development is **paused** at the v0.1.0 checkpoint (2026-10-20). Version 0.1.0 was
> prepared as a candidate for one client. DOCX to PDF conversion and the Windows bundle have been
> checked as described under *Verification status*. There is no installer, the Windows bundle is
> unsigned and not distributed publicly, the application has no licence yet (a draft of client terms
> exists, unapproved) and the self-update feature is not set up in this build. See
> [`docs/STATUS.md`](docs/STATUS.md) for what is verified and what is open, and
> [`docs/PLAN.md`](docs/PLAN.md) for the roadmap (nothing in it is scheduled).

## What it does today

- Select one or more `.docx` files and an output folder.
- Converts them to PDF one at a time in the background; the window stays responsive.
- Shows progress, a per-file result (converted / failed with a reason) and a final summary.
- **Never overwrites** an existing file: if `report.pdf` already exists the new file is saved as
  `report (1).pdf` (then `(2)`, …) and the results table says so.
- **Never modifies your original documents.** Each document is copied into a private temporary
  folder and LibreOffice converts the copy.
- Detects a missing LibreOffice installation and explains how to install it.
- Rejects invalid inputs with a clear reason (missing file, wrong file type, empty file, damaged
  or password-protected `.docx`, Word's `~$` lock files) without stopping the rest of the batch.
- Reports LibreOffice failures, missing/invalid output and timeouts (120 s per document).
- If the system temporary folder is missing, not writable or full, reports a clear error that
  mentions the temporary folder. Neither the message nor the log contains its path.
- **Does not load images that a document links to** (by web address or local file path);
  only images stored inside the `.docx` appear in the PDF. Other kinds of external reference
  have not been tested. See *Privacy design*.
- **Says when linked images were left out.** After a conversion, the results table shows how
  many images in that document were left out because they are only linked (by web address or
  file path) and are never fetched. Only document structures that have been checked with the
  real LibreOffice are detected, so this is not a complete check; see *Known limitations*.
- "Open output folder" button.
- Cancel stops the batch after the document currently being converted.
- "About and licences…" shows that Qt is used under the LGPL-3.0 and where the notices are.

Not implemented (possible future work, see the plan): other input formats (`.doc`, Excel,
PowerPoint, images), PDF merge/split/compress/edit, OCR, a Windows installer, code signing and
a public release. You can build an unsigned Windows bundle yourself; see
*Building a Windows bundle*.

## Requirements

- Windows 10/11 (primary target). The code is written to be cross-platform, but it has only
  been run and tested on Windows 11.
- Python 3.11 or newer. Verified locally with 3.14 (unit and integration tests). CI passed lint
  and the unit tests on 3.11, 3.13 and 3.14 for commit `b6d4549` (Milestone 1); see
  *Development and testing*.
- [LibreOffice](https://www.libreoffice.org/download/), verified with 26.2.1. It is a
  **separate prerequisite**: the app never downloads or installs it. The privacy hardening
  described below relies on a LibreOffice setting that has only been verified with 26.2.1;
  re-run the integration tests after upgrading LibreOffice.

## Windows setup

1. Install Python 3.11+ from <https://www.python.org/downloads/> (tick *Add python.exe to PATH*).
2. Install LibreOffice from <https://www.libreoffice.org/download/> using the default location
   (`C:\Program Files\LibreOffice`). If you install elsewhere, set the environment variable
   `OFFLINE_DOC_TOOLS_SOFFICE` to the full path of `soffice.exe`, e.g.
   `D:\Apps\LibreOffice\program\soffice.exe`.
3. In PowerShell, from the project folder:

   ```powershell
   python -m venv .venv
   .venv\Scripts\python -m pip install -e .
   ```

   These setup steps download Python packages (`PySide6-Essentials`) from PyPI. After setup,
   the app itself needs no internet connection.

## Usage

```powershell
.venv\Scripts\python -m offline_doc_tools
```

or, after installation, the `offline-doc-tools` command in `.venv\Scripts\`.

1. **Add files…** and pick one or more `.docx` files.
2. **Choose…** an output folder.
3. **Convert to PDF**. Results appear in the table; the summary line shows the totals.
4. **Open output folder** to see the PDFs.

To run a locally built bundle instead of the Python environment, start
`dist\OfflineDocTools\OfflineDocTools.exe` (see *Building a Windows bundle*).

## Building a Windows bundle

You can build a folder that contains its own Python runtime and is intended to run the app
without a Python installation. It is a **locally built, unsigned bundle**, not an installer:
there is no code signing and no public download. It can update itself from a private
release repository, but only when a user clicks and confirms (see *Updates*), and only in a
build whose owner has configured that. The bundle is not distributed anywhere.

### Prerequisites

To **build** it:

- Windows (see *Requirements*) and Python 3.11 or newer (the `python` command must start it).
- Internet access during the build: the script downloads the pinned package versions from
  PyPI. This is the only network use in the build-and-check workflow. The app itself needs no
  network (see *Privacy design*).

To **run** it:

- A separately installed LibreOffice (see *LibreOffice is not included* below).

### Build

From the project folder, in PowerShell:

```powershell
python scripts\packaging\build_windows.py
```

The script creates an isolated virtual environment in `build\packaging-venv\`, installs exact
pinned versions (the `build` extra in `pyproject.toml`) and runs PyInstaller with the committed
spec file `scripts\packaging\offline_doc_tools.spec`.

`--vc-runtime-prerequisite` leaves Microsoft's Visual C++ runtime DLLs out of the bundle, so the PC
must have Microsoft's *Visual C++ Redistributable (x64)* 14.44 or newer installed. Without it the
bundle contains those DLLs (copied from the Qt and Python packages), and it has **not** been
established that the owner may redistribute those exact files; see `docs/DISTRIBUTION.md`. The
package delivered to a client is built with this option.

### Output

- `dist\OfflineDocTools\OfflineDocTools.exe`: the application, windowed (no console window).
  The bundle is a folder ("onedir"). Keep `dist\OfflineDocTools\` together and copy the whole
  folder, not only the `.exe`.
- `dist\OfflineDocTools\THIRD-PARTY-NOTICES.txt`: beside the EXE; the notice that the program uses Qt
  and PySide6 under the LGPL-3.0, and the copyright and licence notices of the third-party code in
  the bundle (Qt's own bundled libraries, OpenSSL, Python, Microsoft runtime), taken from the
  upstream sources named in it.
- `dist\OfflineDocTools\licenses\`: the full texts of the LGPL-3.0, GPL-3.0, Apache-2.0 and the
  Python licence, and Qt's commercial-licence notice. The LGPL, GPL and Apache texts and the notices
  file come from `scripts\packaging\licenses\` (committed, never downloaded at build time); the
  Python licence and Qt's notice are copied from the build environment. The build rejects a bundle
  without them.
- Size of the folder (build of 19 Oct 2026, with `--vc-runtime-prerequisite`): 37 files,
  60,345,265 bytes (57.5 MiB).

`build\` and `dist\` are listed in `.gitignore` and are never committed.

### What is and is not included

Included: the application, a Python runtime and, of Qt's modules, only QtCore, QtGui and
QtWidgets (together with the PySide6/shiboken libraries and Qt plugins needed to run them).

Not included:

- **LibreOffice** (see below).
- With `--vc-runtime-prerequisite`: Microsoft's Visual C++ runtime DLLs (install the Redistributable).
- Qt's network module and Qt WebEngine (Chromium).
- Any document files.
- An installer, code signing, or any update that happens without a click and a confirmation.
  (The optional, click-started updater is described under *Updates*.)

### LibreOffice is not included

LibreOffice stays a separate prerequisite: the bundle neither contains nor downloads it. Install
it as described under *Windows setup* (default location, or set `OFFLINE_DOC_TOOLS_SOFFICE` to
the full path of `soffice.exe`). The bundle finds it the same way the source version does. Only
LibreOffice 26.2.1 has been verified (see *Requirements*).

### Windows SmartScreen and Defender

The executable is not code-signed. Expect Windows SmartScreen and Microsoft Defender to show
warnings or prompts for a freshly built bundle (for example "Windows protected your PC"); their
behaviour on other machines has not been checked. Only run a bundle that you built yourself
from this repository.

### Not distributed publicly

The bundle is built on your own machine and is not published: there is no release, download or
installer. The application itself has no licence yet (see *Licence*), which blocks public
distribution.

### Checking a built bundle (smoke test)

```powershell
python scripts\packaging_smoke\smoke_test.py [--app-dir DIR]
```

The script starts a **copy** of the bundle in Qt's offscreen mode, in a sanitised environment
that contains no Python or virtual environment, and checks that:

1. the start-up line is written to the application's log;
2. the process stays alive;
3. no network endpoints are opened;
4. forbidden files are absent (Qt network and WebEngine files, and documents);
5. the licence files are present;
6. the app still starts when LibreOffice cannot be found.

`--app-dir DIR` selects the bundle folder to test; the default is `dist\OfflineDocTools`.

This checks one short start-up. It is not a security audit and not a conversion test, and it
cannot show that no network connection is ever made. Results are recorded under *Verification
status*.

## Development and testing

```powershell
.venv\Scripts\python -m pip install -e ".[dev]"
.venv\Scripts\python -m pytest                       # all tests
.venv\Scripts\python -m pytest -m "not integration"  # skip the real-LibreOffice tests
.venv\Scripts\ruff check .
.venv\Scripts\ruff format --check .
```

- Unit tests replace the LibreOffice process with fakes and run without LibreOffice.
- `tests/integration/` converts synthetic documents with the real LibreOffice and is skipped
  automatically if LibreOffice is not found. It includes the linked-image privacy regression
  test and a real timeout-then-recover test.
- All test documents are generated by the tests. Never add real documents to the repository.
- GitHub Actions (`.github/workflows/ci.yml`) runs lint and the unit tests on `windows-latest`
  with Python 3.11, 3.13 and 3.14. LibreOffice is not installed on the runner, so the
  integration tests are skipped there; they have been run locally only. Run
  [39093737555](https://github.com/StevefromSpace/OfflineTool/actions/runs/39093737555) on
  commit `b6d4549` (Milestone 1) completed successfully. The CI runs for the later changes are
  not recorded in this document; check the Actions tab of the repository for the latest run.

### Code layout

| Path | Responsibility |
|---|---|
| `src/offline_doc_tools/main_window.py` | Desktop UI |
| `src/offline_doc_tools/worker.py` | Background conversion on a `QThread` |
| `src/offline_doc_tools/service.py` | Validation, temp workspace, PDF checks, safe output naming |
| `src/offline_doc_tools/engine.py` | Converter interface and error types |
| `src/offline_doc_tools/libreoffice.py` | LibreOffice discovery and invocation |
| `src/offline_doc_tools/docx_sanitize.py` | Removes or refuses external references in the private copy |
| `src/offline_doc_tools/docx_inspect.py` | Counts linked-only images for the results notice |
| `src/offline_doc_tools/updater/` | Click-started self-update: signed manifest, private GitHub client, safe staging, replacement helper (`docs/UPDATES.md`) |
| `src/offline_doc_tools/app.py` | Entry point and local logging |
| `scripts/packaging/build_windows.py` | Builds the Windows bundle in an isolated virtual environment |
| `scripts/packaging/offline_doc_tools.spec` | Committed PyInstaller specification of the bundle |
| `scripts/packaging_smoke/smoke_test.py` | Smoke test for a built bundle |

## Privacy design

- No cloud services, no uploads, no accounts, no telemetry or analytics, no crash reporting,
  no automatic update checks. Converting documents never uses the network. The one exception
  is the optional **Updates** feature below, which runs only when you click its buttons. The
  Windows bundle is built without Qt's network module and Qt WebEngine.
- **Updates (optional; every step needs a click, installing needs your confirmation).** The
  *Check for updates* button asks a **private** GitHub repository for its latest release, using a
  read-only access token that the user typed in (stored encrypted for the Windows account with
  DPAPI, sent only to `api.github.com`, never built in or logged). A newer version is believed only if
  its manifest carries a valid Ed25519 signature from a key built into the application; the package
  is then checked against the signed size and SHA-256 and against archive safety rules before it is
  extracted, and it is installed by a separate helper that keeps a backup and restores it if the new
  version does not start. Requests contain the token, a fixed `User-Agent` and nothing else: no
  document, file name, path, conversion data, version or system information. GitHub sees the user's IP
  address and the token's account. Without a configured repository and key, and without a token, the
  application makes no request at all; in a build without them (including the v0.1 client build)
  the update buttons are hidden. Details and limits: `docs/UPDATES.md`. **Not yet verified
  against a real private repository, token or the production signing key.** The Windows bundle
  includes Python's TLS support (OpenSSL 3) for this feature only.
- **Linked images are blocked.** A `.docx` can *link* to an image by web address or local
  file path instead of containing it. With default settings, LibreOffice 26.2.1 downloaded
  such web images during conversion (a network request that also tells the document's sender
  when and from where it was opened) and copied linked local files into the PDF. The app
  therefore starts LibreOffice with a profile that enables its `BlockUntrustedRefererLinks`
  setting before every conversion; linked images are then left out of the PDF instead.
  Images stored inside the document are still converted. Only images were verified: other
  kinds of external reference (linked OLE objects, `INCLUDETEXT` fields, remote templates)
  have not been tested. After a conversion the results table says how many linked images were
  left out; see *Known limitations* for what that notice does and does not cover.
- **The private copy is sanitised before LibreOffice sees it.** `docx_sanitize.py` removes every
  relationship that points outside the document (except hyperlinks that the document uses only as
  links, which stay clickable in the PDF) and **refuses to convert** a document that contains an
  embedded HTML/text section (`w:altChunk`), an SVG picture that names files outside the document
  (also when gzip-compressed), or anything it cannot fully check (damaged, encrypted, a DOCTYPE,
  unusually large). A refused document is never passed to LibreOffice, and your original file is
  never changed. These two refusal cases were observed with LibreOffice 26.2.1 on 18 Oct 2026
  (it fetched the HTML section's images and embedded files named by an SVG); the probe logs were
  not saved. Field codes other than the ones probed, nested packages (embedded OLE objects) and
  anything else LibreOffice might load without a relationship are untested.
- LibreOffice runs with `--headless` and a **fresh, temporary user profile**, so it does not
  touch your normal LibreOffice settings or recent-documents list; the profile is deleted
  when the batch ends (and replaced mid-batch if a conversion had to be stopped).
  LibreOffice's own temporary files are also directed into that profile, so they are deleted
  with it.
- Temporary copies of documents live in a private folder from Python's `tempfile` module
  (under `%TEMP%`, named `offline-doc-tools-*`). The throw-away LibreOffice profile, which
  also receives LibreOffice's own temporary files, lives in a second folder there
  (`odt-profile-*`). Each copy is deleted as soon as that document is done, and both folders
  when the batch ends. If the application is killed, or a file is still locked, a folder of
  either name may remain (for a locked file a warning is logged). A leftover folder may still
  contain document data and can be deleted manually. If the temporary folder is missing, not
  writable or full, the app reports an error that mentions the temporary folder but does not
  print its path.
- A small diagnostic log (max ~1 MB, rotated) is kept at
  `%LOCALAPPDATA%\OfflineDocTools\logs\app.log`. It records job numbers, outcomes, error
  types and LibreOffice exit codes, plus LibreOffice's output (up to 2,000 characters) when
  a conversion fails. It never records document contents or your file names, not even their
  extensions (LibreOffice only ever sees a temporary copy called `source.docx`), and
  temporary-folder paths are replaced by placeholders such as `<workspace>` so your Windows
  user name is not written either. The exception is an *unexpected* internal error, whose
  technical traceback could include a file path.
- The app itself stores no settings or recent-file list. The standard Windows file dialogs
  remember recently used folders, as they do for any application.

**Verification status** (Windows 11, LibreOffice 26.2.1). What was actually checked:

- Source review (Milestone 1, before the updater was added): no networking code in the
  application; Qt's network module is never loaded. The updater is the one later addition.
- Runtime: a GUI-driven batch conversion with a Python audit hook recorded **no** socket/DNS/URL
  activity in the application process, and polling every 100 ms found no network endpoints
  owned by LibreOffice processes. Polling can miss very short-lived connections.
- The linked-image test (`tests/integration/`) converts two documents in one profile, each
  with an embedded image, an image linked to a local loopback web server and one linked to a
  local file. With the hardened profile LibreOffice made no request, kept the embedded image
  and left out both linked ones; with the setting removed it requested the image and the
  test fails.
- One-off manual observation, not an automated test and not repeated: an image linked by UNC
  path (`\\127.0.0.1\share\...`) was seen to cause no SMB connection (port 445) during
  conversion, with or without the hardened profile. The watcher used for this detected a
  connection when the same path was opened directly (positive control). Only the log of that
  positive control was saved, not the logs of the conversion runs, so the observation cannot
  be re-checked from saved evidence.
- CI (lint and unit tests only): run
  [39093737555](https://github.com/StevefromSpace/OfflineTool/actions/runs/39093737555) on
  commit `b6d4549` (Milestone 1) completed successfully on `windows-latest` with Python 3.11,
  3.13 and 3.14. The integration tests were skipped there because LibreOffice is not
  installed on the runner; they have been run locally only.
Windows bundle, build of 19 Oct 2026 (Windows 11 build 26300, Python 3.14.5, PyInstaller 6.22.2,
PySide6-Essentials and shiboken6 6.12.0, LibreOffice 26.2.1.2, built with
`--vc-runtime-prerequisite`; the build machine has the 14.51 Visual C++ Redistributable):

- Smoke test: all checks passed (layout and licence files, no forbidden content, start-up logged
  after 1.6 s, still running, no network endpoint, starts without LibreOffice, clean-up).
- Conversion by the packaged app: a synthetic `.docx` with spaces in its name was added through
  the real Windows file dialog, an output folder was chosen, *Convert to PDF* was clicked, and the
  window reported `Finished: 1 converted, 0 failed.`. The PDF was valid (`%PDF-` header and
  trailer, 22,786 bytes) and the source document was unchanged. The window was driven by a script
  (Windows UI Automation and window messages) on the build machine.
- Update mechanism, run on real packaged bundles against a local stand-in for GitHub and a
  throw-away test key (not the real private repository or key): an update signed with the test key
  was installed by the new package's own helper, an obsolete file was removed, the installation
  matched the new bundle file by file, and an update that fails to start was rolled back to an exact
  copy of the previous installation (16 checks).
- Tests on the source of this build: 991 unit tests passed (with `openssl` available), 6
  integration tests with the real LibreOffice passed, and `ruff check` and `ruff format --check`
  were clean.

Not verified: a run with the machine's network physically disabled or blocked by firewall,
and LibreOffice's network behaviour beyond the scenarios above. For the Windows bundle also
not verified: running it on a separate clean machine (the smoke test's sanitised environment
on the machine that built it is not the same thing), the behaviour on a PC without the Visual C++
Redistributable (the program is expected not to start), SmartScreen and Defender behaviour, and
the build-time network use (pip downloads are the only intended network use; it has not been
measured). The update feature has not been used against a real private repository, token or the
production signing key.

## Known limitations

- Layout fidelity depends on LibreOffice; fonts missing from the machine are substituted.
- Cancel waits for the current document to finish (or time out).
- Linked (not embedded) images are left out of the PDF, by design (see *Privacy design*).
  The results table says how many were left out, but only for the document structures that
  have been checked with the real LibreOffice. The notice is not a complete check: a missing
  notice does not prove that nothing was left out. The only structure detected is a picture
  (`a:blip`) whose `r:link` points to an external image relationship, in any part of the
  document (inline pictures, headers, linked shape fills).
- Other external relationships that are not hyperlinks (for example an attached template or
  other linked data) are removed from the private copy without a notice in the results table;
  only a count is written to the log.
- Only linked *images* have been tested. Other external references (linked OLE objects,
  `INCLUDETEXT` fields, remote templates) are untested.
- Password-protected documents are not supported.
- There is no input size limit; a very large or complex document is stopped by the
  120-second timeout.
- LibreOffice 26.2.1 crashes when its profile folder path is too deep. The app keeps the
  profile directly in the system temporary folder with short folder names; conversions
  worked with temporary-folder paths up to 120 characters and failed from about 125. A
  typical `%TEMP%` (`C:\Users\<name>\AppData\Local\Temp`) is far shorter. If every
  conversion fails with "LibreOffice crashed", point `TEMP`/`TMP` to a shorter folder.
- No installer, no code signing and no public release. Run from source, the app needs a
  Python environment. A locally built Windows bundle is intended to run without one (see
  *Building a Windows bundle*), but it is unsigned, needs a separately installed LibreOffice
  and has only been checked as described under *Verification status*.

## Licence

**The application itself has no licence yet.** Nothing in this repository grants anyone permission to
use, copy, modify or distribute it, and public distribution of the Windows bundle stays blocked until
the owner decides. A draft of proposed client terms exists outside this repository; it is not approved.

Third-party components keep their own licences and are not affected by the application's licence.
`THIRD-PARTY-NOTICES.txt` and `licenses\` in the bundle carry their notices and texts. Qt and PySide6
(Qt for Python) are used under the **LGPL-3.0**; their source code, the notice requirements still open
and the other distribution questions are recorded in `docs/DISTRIBUTION.md`. That document also
states what has and has not been verified; it is not legal advice.
