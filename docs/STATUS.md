# Project status: OfflineTool v0.1 (paused checkpoint)

Status as of 2026-10-20: **development is paused at the v0.1 checkpoint.** v0.1 is not released and
is not approved for public or client distribution. This file separates what was verified (with the
date) from what is still open. It is an engineering record, not legal advice.

## Why development is paused

Stirling PDF was identified as a mature, broader solution aligned with the direction the project
wanted. The decision is to stop here and not to expand OfflineTool into a competing full
document/PDF platform. Updater and gateway development has stopped. No new features are planned.
`docs/PLAN.md` lists possible extensions; none is scheduled.

## What v0.1 contains

- A Windows desktop app (PySide6) that converts `.docx` files to PDF with a locally installed
  LibreOffice. Nothing is uploaded, and the conversion path makes no network requests.
- A UI-independent service layer: validation, a private copy of each input, collision-safe output
  naming (an existing file is never overwritten), and PDF verification.
- Hardened LibreOffice invocation: throw-away profiles, linked images neither fetched nor embedded,
  output redaction, timeouts with process-tree kill. Document sanitization and hardening work is
  implemented (`docx_inspect.py`, `docx_sanitize.py`).
- A Windows bundle built with PyInstaller, with third-party notices, LGPL and GPL texts, and an
  **About and licences…** button.
- A click-started updater, present in the source but **disabled**: `UPDATE_REPOSITORY` and
  `TRUSTED_PUBLIC_KEYS` are empty, and the packaged window shows no update controls. Its real chain
  has not been tested against any private repository, token or production key.

## Final client package

| Item | Value |
|---|---|
| File | `dist\release\OfflineDocTools-0.1.0-win64-client-final.zip` |
| Copy | `dist\verified\OfflineDocTools-0.1.0-win64-client-final.zip` |
| Size | 25,520,615 bytes |
| SHA-256 | `f41f3f705d2bc221e7949464fc7ceb3fd26d2ffc9823b8dd19872769b773f463` |
| Executable inside | `OfflineDocTools.exe`, SHA-256 `3fc99bfbc9e797106c90ee1cd4f0c45ad7005fc0b8c22ecae779f4a65b4cb541` |
| Prerequisite | Microsoft Visual C++ Redistributable (x64) 14.44 or newer. The ZIP contains no Microsoft runtime DLLs. |

On 2026-10-20 both copies were hashed again and match the SHA-256 above. The ZIP was not rebuilt or
modified. `dist\` is gitignored and the ZIP is not in the repository.

## Verified facts

Recorded in `docs/PLAN.md` (verification record) and `docs/DISTRIBUTION.md`, from 2026-10-19:

- 997 tests pass: 991 unit tests and 6 integration tests against the real LibreOffice 26.2.1.
- `ruff check` and `ruff format --check` are clean.
- The bundle smoke test passes 8 of 8 checks, on the bundle and on the extracted final ZIP.
- A real conversion through the windowed app, driven by script, succeeded on a copy of the bundle
  and on the extracted final ZIP. The source document was unchanged.
- The packaged updater controls are hidden and unconfigured.
- The ZIP contains none of the ten Microsoft runtime files (checked by file name).

Carried forward, **not executed again for this finalisation**: the packaged update and rollback test
(16 checks, with a local stand-in for GitHub and a throw-away key). No tests were re-run on
2026-10-20, because no source code changed.

## Unresolved technical verification

- A clean Windows machine without the Visual C++ Redistributable has not been tested, so "the program
  does not start there" is an expectation, not a result.
- Nothing proves that the shipped PySide6 and Qt binaries were built from the staged source archives.
  The archives match Qt's published checksums, and the shipped `Qt6Core.dll` reports version 6.12.0.
  That shows consistent version numbers, not identical builds.
- The updater has not been tested against a real private repository, token or signing key.
- The executable is unsigned. SmartScreen and Defender behaviour was not tested.
- LibreOffice is verified only with version 26.2.1. The integration tests run locally only.
- No independent review of the final tree has been done.

## Unresolved licensing and legal decisions

- **The client licence is an unapproved draft**, kept outside this repository. It has
  placeholders for the licensor, client, term, jurisdiction and liability, and it has not been
  reviewed by counsel. It must keep the LGPL carve-out.
- **LGPL-3.0 review** by counsel: installation information (section 4e), patent terms, and whether
  the window notice and `THIRD-PARTY-NOTICES.txt` are enough.
- **Corresponding source:** the owner must confirm the separate source folder as the delivery route,
  or choose a written offer. No offer has been written.
- **Microsoft runtime:** the prerequisite route is built and documented. Redistribution rights for
  the DLLs have not been established, so they are not shipped.
- **Application licence:** the application has no licence. Nothing grants anyone permission to use,
  copy, modify or distribute it.
- **Distribution:** nothing is approved for public or client distribution.

## Repository and release state

- Source repository: `https://github.com/StevefromSpace/OfflineTool`, branch `main`. The repository
  is public and has no licence file.
- No GitHub Release or tag exists for v0.1, and the client ZIP is not published anywhere.
- The commit that adds this document is the v0.1 checkpoint. See `git log` for its hash.

## If development resumes

Start from `docs/PLAN.md`, "Potential future extensions". Do not expand OfflineTool into a
competing full document/PDF platform: that scope is covered by Stirling PDF. Settle the legal
questions above before any further distribution.
