# Distribution notes: third-party components and open licensing questions

Prepared 2026-10-19 against Build B/C of the Windows bundle (OfflineDocTools 0.1.0) and updated the
same day for the final client build (`OfflineDocTools-0.1.0-win64-client-final.zip`; sections 2, 4, 5
and 7). This is an engineering record of what was checked and what was not. **It is not legal advice and does not
claim that the bundle complies with any licence.** Files are named relative to
`dist\OfflineDocTools\`.

## 1. What is actually in the bundle (read from the shipped files)

| Component | Version found | Licence (upstream) | Where | Notice / text in the bundle |
|---|---|---|---|---|
| Qt (Core, Gui, Widgets, platform and style plugins) | 6.12.0 (DLL metadata) | LGPL-3.0-only chosen from LGPL-3.0 OR GPL-2.0 OR GPL-3.0 OR commercial | `_internal\PySide6\Qt6*.dll`, `plugins\` | `THIRD-PARTY-NOTICES.txt`, `licenses\LGPL-3.0.txt`, `GPL-3.0.txt` |
| Third-party code inside Qt (PCRE2, zlib, libpng, FreeType, HarfBuzz, md4c, D3D12MA, VMA, Unicode data, Wintab, ...) | per Qt 6.12.0 attribution files | BSD, MIT, FTL, Zlib, Apache-2.0, Unicode-3.0, ... | inside the Qt DLLs | `THIRD-PARTY-NOTICES.txt` section 2 (texts verbatim from qtbase `v6.12.0`) |
| PySide6 / shiboken6 | 6.12.0 (wheel metadata, source headers) | LGPL-3.0-only chosen (same options) | `QtCore/QtGui/QtWidgets.pyd`, `pyside6.abi3.dll`, `Shiboken.pyd`, `shiboken6.abi3.dll` | same as Qt |
| OpenSSL | 3.0.20 (DLL metadata) | Apache-2.0; upstream tag `openssl-3.0.20` has no NOTICE file | `libssl-3.dll`, `libcrypto-3.dll` | `licenses\OpenSSL-Apache-2.0.txt` |
| Python runtime | 3.14.5 | PSF 2.0 | `python314.dll`, `.pyd` modules, `base_library.zip` | `licenses\Python-LICENSE.txt` (python.org's file: also bzip2, libffi, Zstandard, an Apache text, the Microsoft Distributable Code notice) |
| Python-incorporated code not in that file | per CPython 3.14.5 `Doc/license.rst` | MT (BSD), SipHash24, dtoa, expat (MIT), zlib, cfuhash, mimalloc (MIT) | `python314.dll`, `pyexpat.pyd`, `_elementtree.pyd` | `THIRD-PARTY-NOTICES.txt` section 4 |
| xz / liblzma | in `_lzma.pyd` | public domain | | none required |
| Microsoft Visual C++ runtime | 14.44.35211.0 (beside Qt and shiboken6) and 14.42.34438.0 (top level) | Microsoft redistributable-code terms | `MSVCP140*.dll`, `VCRUNTIME140*.dll` | statement only, see section 4 |
| PyInstaller bootloader and run-time hooks | 6.22.2 | GPL-2.0-or-later with Bootloader Exception; hooks Apache-2.0 | inside `OfflineDocTools.exe` | `THIRD-PARTY-NOTICES.txt` section 5 |

Not present, so no notice is shipped for them: ICU (Qt links the Windows system `icuuc.dll`),
libjpeg, SQLite, Qt Network, OpenGL software renderer, Tcl/Tk, libmpdec.

How "compiled in" was decided: for FreeType, HarfBuzz, libpng, PCRE2, zlib, D3D12MA and Wintab a
signature was found in the shipped binary. For the rest (hashes, double-conversion, TinyCBOR,
Unicode data, ...) the entry follows Qt's own attribution file for a default build; the binary
was not inspected for each. Entries marked that way in the notices say so.

## 2. Qt / PySide6 under the LGPL-3.0

Sources: the LGPL-3.0 text, Qt's pages https://doc.qt.io/qt-6/lgpl.html and
https://www.qt.io/development/open-source-lgpl-obligations (fetched 2026-10-19).

| Requirement | Status | Evidence / what is missing |
|---|---|---|
| A copy of the LGPL and the GPL it incorporates accompanies the program (LGPL 4b) | Done | `licenses\LGPL-3.0.txt`, `GPL-3.0.txt`: verbatim from gnu.org, LGPL identical to SPDX, GPL differs only in appendix URLs. Required by `bundle_policy.py` |
| Copyright notices and third-party attributions | Done for what could be derived | `THIRD-PARTY-NOTICES.txt`, generated from upstream records. Not independently audited |
| Prominent notice that the Library is used and covered by the LGPL (4a); Qt: "not allowed to hide the fact" | Done in two places | The first paragraph of `THIRD-PARTY-NOTICES.txt` beside the EXE, and (added for the final build) the window's **About and licences…** button, which says Qt and PySide6 are used under the LGPL-3.0 as separate library files and where the notices are (`about_text()` in `main_window.py`, `tests/test_about_notice.py`; button seen in the packaged window). Whether this is enough is for counsel |
| Corresponding source delivered, or a written offer; Qt: required "even if the library has not been modified at all" | **Route prepared; owner confirms** | The archives are supplied with the delivery as a separate folder with checksums and a plain description (section 5), which avoids a written offer (named contact, multi-year commitment). No offer has been written or invented. If the folder is not delivered, a written offer or equivalent access under GPLv3 section 6 would still be needed |
| Source corresponds to the shipped binaries | Unverified | The DLLs are Qt's own build for the PyPI wheels. Their build configuration and any patches are not published in a form checked here. The archives are the official 6.12.0 releases. Re-checked on 2026-10-19: both archives match the recorded SHA-256 values, the qtbase MD5 equals Qt's published value, and the shipped `Qt6Core.dll` reports version 6.12.0.0. That is consistency of version numbers, not proof of identical builds |
| User can replace or relink the libraries (4d) | Mechanically verified; a recompiled Qt was not tried | Import tables: every Qt binding imports `Qt6*.dll` / `pyside6.abi3.dll`; the EXE holds no Qt code. A packaged copy with a binary-**modified** `Qt6Core.dll` (one character of its version resource changed, so a different file hash) started, passed the 8 smoke checks and answered a UI click, so nothing checks the DLLs' integrity. No patch-level Qt 6.12.x release exists on PyPI to substitute (only 6.12.0 and older series), and a Qt built from the source archives was not made |
| Terms of the program "effectively do not restrict modification of the portions of the Library ... and reverse engineering for debugging such modifications" (4) | **Open: must be in the client licence** | The draft licence carries a carve-out. Qt: the LGPL freedoms "cannot be in any way negotiated upon or restricted" |
| Installation Information (4e) | Probably not applicable, unconfirmed | Applies only where GPLv3 section 6 requires it (user products). A business desktop program is, on my reading, not one. Needs counsel |
| LGPL-3.0 patent and other terms | Not assessed | Qt recommends legal review |

## 3. OpenSSL

3.0.20 (file version and copyright "1998-2026 The OpenSSL Authors" read from the DLLs), licence
Apache-2.0 (upstream `LICENSE.txt` at tag `openssl-3.0.20`), text shipped, no NOTICE file upstream.
The DLLs are python.org's builds; whether they carry patches was not checked. The Apache text
asks that modified files carry notices; none are known.

## 4. Microsoft Visual C++ runtime: not established for this build; alternative tested

Microsoft (https://learn.microsoft.com/en-us/cpp/windows/redistributing-visual-cpp-files): "Distribution of
the Visual C++ Runtime Redistributable package, merge modules, and individual binaries is limited to
licensed Visual Studio users and is subject to Microsoft Software License Terms", and only files on
Microsoft's REDIST list may be redistributed. Central deployment of the Redistributable package is
"preferred", and the Redistributable must be the same version as, or newer than, the toolset the
software was built with. python.org's licence file (shipped) adds: anyone further distributing
programs that include this code "must require distributors and external end users to agree to terms
that protect the Microsoft Distributable Code at least as much as Microsoft's own requirements".

What was checked on 2026-10-19:

* The bundle holds five distinct Microsoft files in ten copies: `MSVCP140.dll`, `MSVCP140_1.dll`,
  `MSVCP140_2.dll`, `VCRUNTIME140.dll`, `VCRUNTIME140_1.dll`, versions 14.44.35211.0 (inside the Qt
  and shiboken6 folders, from the PySide6 wheel) and 14.42.34438.0 (top level, from the python.org
  installation).
* This machine has Visual Studio Build Tools 2022 (17.14) with its own `VC\Redist\MSVC\14.44.35112`
  folder. **None of the bundled files is byte-identical to the files in that licensed REDIST folder**
  (different hashes and a different, older build number than the Qt DLLs), so the owner's own build
  environment cannot be used to show that these exact files may be redistributed. I did not obtain
  the Build Tools licence text (the URL I guessed returned 404), so whether it grants distributable-code
  rights at all is also unknown.
* Alternative (Microsoft's preferred route): leave the DLLs out and require the Redistributable.
  `build_windows.py --vc-runtime-prerequisite` does this. A copy of the bundle without any of the ten
  files passed all 8 smoke checks on this machine, **which has the 14.51 Redistributable installed**.
  A PC without the Redistributable was not available to test; there the program would not start (the
  loader reports the missing `VCRUNTIME140.dll`), which is why the client instructions name it.

Decision for the owner: ship without the DLLs and require the Redistributable (recommended; the
delivery candidate is built that way), or ship them after confirming the owner's rights and adding
Microsoft-protective terms to the client licence.

Final client build (2026-10-19): built with `--vc-runtime-prerequisite`; the final ZIP (39 files)
contains none of the ten Microsoft runtime files (checked by file name in the extracted copy), and
`START-HERE.md` tells the client to install the Redistributable (x64) 14.44 or newer. The smoke test
and a real conversion passed on this machine, which has the 14.51 Redistributable. A PC without it
was still not available, so "the program does not start there" remains an expectation, not a result.

2026-10-19 (later): the clean-machine test could not be done on the owner's PC (Windows 11 Home has no
Windows Sandbox or Hyper-V, and the installed Redistributable lives in System32). It needs a separate PC or VM;
see the owner's private handover checklist, which is kept outside this repository.

## 5. Corresponding source (prepared as a separate delivery folder)

`dist\corresponding-source\` (gitignored, unextracted):

| File | Bytes | SHA-256 | Checked against |
|---|---|---|---|
| `qtbase-everywhere-src-6.12.0.tar.xz` | 51,220,268 | `a951bd163c7b80fc6b8c88d7668fb56abf91c152373e13c10666763238131307` | MD5 `48467e8622aa17c25fa39418092b908c` equals Qt's published `md5sums.txt` |
| `pyside-setup-everywhere-src-6.12.0.tar.xz` | 18,211,772 | `099c1a597cf33c1b000e11cf68a0b82d216fb802bf81726efc720394f10c7751` | no checksum is published (404); size equals the server's |

Contents were listed without extracting: qtbase holds `src/corelib`, `src/gui`, `src/widgets`,
`src/plugins/platforms/{windows,offscreen}`, `src/plugins/styles/modernwindows` and `LICENSES/`;
pyside-setup holds `sources/pyside6`, `sources/shiboken6`, `build_scripts` and `LICENSES/`.
A written offer (GPLv3 section 6b) would need the distributor's name and contact, a term of at least
three years, and a way to obtain the source; none of that has been invented here.

Delivery folder (assembled 2026-10-19, not part of the client ZIP):
`dist\release\OfflineDocTools-0.1.0-corresponding-source\` holds the two archives above,
`qt-6.12.0-md5sums.txt`, `SHA256SUMS.txt` and `SOURCE-README.txt`. The README states what is and is
not established (official unmodified releases; no proof that the PyPI binaries were built from exactly
these archives). It is meant to be handed to the client together with the ZIP. It is about 70 MB and
is kept out of the ZIP so that update packages do not carry it.

## 6. Public repository

`https://github.com/StevefromSpace/OfflineTool` is public (checked 2026-10-20), has no licence, no
releases and no tags. Before the v0.1 checkpoint commit, `main` held only Milestone 1 (`b6d4549`); the
Milestone 2 work and this document were local and uncommitted. The checkpoint commit adds them to
`main` (`git log` shows its hash). The client ZIP, the corresponding-source folder and any update
package are not in the repository and must not be published there as releases. Updates use a separate
private repository (`docs/UPDATES.md`).

## 7. Still open before v0.1 can be called legally clean (2026-10-19; development paused 2026-10-20)

Development of OfflineTool is paused at the v0.1 checkpoint (`docs/STATUS.md`). The items below are
not resolved by the pause and remain blockers for any distribution.

1. Owner: confirm the source route (section 5 folder delivered with the ZIP) or choose a written offer.
2. Counsel: LGPL-3.0 review that Qt recommends (4e Installation Information, patents, whether the
   window notice and `THIRD-PARTY-NOTICES.txt` suffice).
3. Client licence: the draft (kept outside this repository) is not approved. It must keep
   the LGPL carve-out (no restriction of modifying the Library and reverse engineering to debug such
   modifications) and name licensor, client, term, jurisdiction and liability; none of that was invented.
4. Microsoft runtime: the prerequisite route is built and documented; delivering the DLLs instead
   would need the owner's redistribution rights to be established.
5. Verification gaps: the PyPI Qt binaries versus the source archives (section 2), OpenSSL "NOTICE"
   expectations (section 3), and the notices file as a whole, which is generated from upstream records
   and not independently audited.

## 8. Re-checking after a change

Regenerate `scripts/packaging/licenses/THIRD-PARTY-NOTICES.txt` whenever the `build` pins in
`pyproject.toml` change (a test fails if the Qt version in the notices differs from the pin), then
`python scripts\packaging\build_windows.py`, `python scripts\packaging_smoke\smoke_test.py`,
`python scripts\packaging\make_release_zip.py --suffix <label>`.
