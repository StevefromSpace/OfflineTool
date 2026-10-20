# Updates: private GitHub Releases, signed, installed by a separate helper

Status (2026-10-19): implemented and tested with fakes and with real packaged bundles on a local
stand-in for GitHub. The packaged OLD -> NEW -> BROKEN run was repeated on the final v0.1 executables
(16 checks passed; NEW's own helper did the replacement, BROKEN was rolled back exactly). **Not yet
exercised against a real private repository, a real token or the production signing key**, because
none of those exist yet (they need the owner, see sections 7 and 8). Until then the updater is dormant:
the build asks for no network at all, and the window hides the update controls.

## 1. What happens

1. The user clicks **Check for updates**. Nothing is ever requested by itself, at start-up or later.
2. The application asks the *private* release repository for its latest published release, using a
   read-only access token the user entered once (**Update access…**).
3. The release must contain `update-manifest.json` and `update-manifest.json.sig`. The manifest is
   believed only if its Ed25519 signature verifies against a public key built into the application.
4. If the signed version is newer, the window says so. **Download and install…** asks for consent.
5. The package is downloaded to a staging folder in the user's private data folder
   (`%LOCALAPPDATA%\OfflineDocTools\update-staging`, not beside the installation, whose parent may be
   writable by other accounts), then checked against the signed size and SHA-256 and against archive
   rules, and only then extracted.
6. The application starts the staged new executable as a helper (`--apply-update`) and quits.
7. The helper waits for the application to exit, renames the installation to a backup, copies the new
   files in, starts the new version and waits for it to report that it came up. Only then is the
   backup deleted. If anything fails, the backup is restored.
8. At the next start the window shows the outcome ("Update to 0.2.0: ..." or "Update failed: ...").

Document conversion never depends on any of this. Conversions are blocked while an update is fetched,
and an update is refused while a conversion runs.

## 2. Why these choices

| Question | Answer |
|---|---|
| Who may read the releases? | A private repository (`UPDATE_REPOSITORY`), separate from the public source repository. Nothing is published to the public one. |
| How does the app authenticate? | A **fine-grained personal access token** with read-only *Contents* permission on that one repository, created by the owner and typed in by the user. It is stored with Windows DPAPI (current user only), sent to `api.github.com` only, never logged and never built in. Clients need **no GitHub account**. |
| Is HTTPS/GitHub enough to trust a package? | No. A token holder, a mistake or a compromised account could change release files. Authenticity comes from the Ed25519 signature; the app ignores every URL and field in GitHub's reply except the plain facts it validates. |
| How does the updater update itself? | The helper is the *new package's own executable*, run from the staging folder. A new updater therefore installs itself with no special case, and the old executable is never overwritten while running. |
| Compatibility | `updater_protocol` in the signed manifest must be one the running version knows (now: 1). `min_installed_version` forces older installations through an intermediate manual update. Unknown manifest formats are refused. |

Alternatives considered: **GitHub App with device flow** needs a GitHub account and repository access
for every user, and the owner must first register the App; it cannot be tested here and is not built.
**Another private endpoint** (for example an authenticated storage bucket) would work with the same
signed manifest and helper, but needs hosting and credentials the owner does not have yet; GitHub
Releases is the simplest thing that is private, authenticated and already available.

## 3. What is checked (and where it is tested)

* Signature: strict Ed25519 (`updater/ed25519.py`; RFC 8032 vectors, OpenSSL cross-check, malformed,
  non-canonical and small-order inputs). The signature covers the exact manifest bytes.
* Manifest: format version, application id, stable channel, version string, package name that must
  equal `OfflineDocTools-<version>-win64.zip`, SHA-256, size cap, protocol, optional minimum version.
  The release tag must equal the signed version. Older or equal versions are never installed.
* GitHub replies: parsed defensively; draft or prerelease releases, odd asset lists and over-large
  replies are refused. Asset URLs are built by the application from the repository name and numeric
  asset id. A download answers with one redirect: only an HTTPS URL on `*.githubusercontent.com` is
  followed, **without the token**; any other redirect is refused.
* Failures: revoked or expired token, no access, rate limit (with the wait), network errors, timeouts,
  missing TLS, oversize and slow downloads, cancel, no disk space; each becomes a short message.
* Package: size and SHA-256 first; then every archive entry (no absolute paths, `..`, drive letters,
  `:` streams, backslashes, reserved device names, trailing dots/spaces, case-insensitive duplicates,
  symlinks, encryption, scripts and shortcuts, size and count limits) and the required layout; files
  are written one by one with exclusive creation.
* Installer: refuses anything that is not an OfflineDocTools installation (the folder must hold only
  the bundle's own top-level items, so a shared folder such as `D:\Tools` or a Desktop is never
  renamed or deleted), a payload inside it, an unwritable parent, too little space or an unknown
  protocol, all before changing anything. It waits for the application to exit *before* it looks at an
  interrupted earlier update; a journal next to the installation lets it undo one, and if that fails the
  update stops. A confirmed new version is never undone because of a bookkeeping error. If even the
  rollback fails, both folders are kept and named in the message. The new application started by the
  helper does not delete the staging folder; that happens at a later start, once a result exists.

Tests: `tests/test_updater_*.py`, `tests/test_update_ui.py`, `tests/test_release_tool.py`, and the
packaged run `scripts/packaging_smoke/update_e2e.py` (section 6).

## 4. Network activity (all of it)

Only after a click: `GET https://api.github.com/repos/<repo>/releases/latest`, the manifest and
signature assets, and (after consent) the package asset, each through `api.github.com` plus one
redirect to a GitHub content host. Requests carry the token, a fixed `User-Agent`
(`OfflineDocTools-updater`), no version, no system information, and nothing about documents, file
names, paths or conversions. GitHub sees the user's IP address and the token's account.

## 5. Releasing (owner)

```
python scripts\release\release_tool.py keygen --key C:\secure\offlinetool-release-key.pem   # once
python scripts\packaging\build_windows.py
python scripts\packaging_smoke\smoke_test.py
python scripts\packaging\make_release_zip.py --suffix ""        # or the name without a suffix
python scripts\release\release_tool.py release --zip dist\release\OfflineDocTools-X.Y.Z-win64.zip ^
    --key C:\secure\offlinetool-release-key.pem
```
Then create a release `vX.Y.Z` in the private repository and upload the ZIP, `update-manifest.json`
and `update-manifest.json.sig`. The tool refuses a package that the application would refuse. The
version in `__init__.py`, `pyproject.toml` and the tag must agree. The private key never enters the
repository, a package or a log; whoever holds it can publish updates to every client.

## 6. Evidence

See the final report of this work and `scripts/packaging_smoke/update_e2e.py`, which installs an OLD
bundle, updates it through the real service to a NEW bundle (whose helper tags its log line), checks
that the installation equals the new bundle file by file with an obsolete file removed, and then
updates it to a BROKEN bundle and checks that the old installation is restored exactly.

## 7. What the owner must do before this works (and what is still open)

1. Create a **private** GitHub repository for releases (not the public source repository).
2. Generate the signing key (`keygen` above) and keep the private key offline; send nobody the file.
3. Put the repository (`owner/name`) and the printed public key into
   `src/offline_doc_tools/updater/config.py`, rebuild, and deliver *that* build to the client by
   hand once. A build without both values can never update itself.
4. Create a fine-grained token (owner account, only that repository, *Contents: read*, an expiry) and
   give it to the client's users. Check GitHub's terms on handing a token to third parties, and decide
   between one token for the client or one per user. Revoking a token cuts access immediately.
5. Verify the real chain once with a real private release (this has not been done).

Limits: updates need Windows write access to the folder that contains the application (install it
somewhere the user can write, not under Program Files); an update cannot be installed while the
application is not the packaged executable; a power failure in the middle of the file swap can leave
`OfflineDocTools.backup-*` next to the application, which a later update (or a manual rename) restores.
A same-user attacker who can write to the installation folder can replace the program directly, so
the staging step defends against network, repository and account compromise, not against that.

Known limits (from the independent review, not fixed): (1) **replay** - someone who controls the
release repository or the token's account but not the key could republish an *older signed* release
that is still newer than the installed one; the manifest has no expiry. Keep the repository account
secured with two-factor authentication, and bump `min_installed_version` when a release must not be
reachable. (2) The helper can be started by any process of the same user with any bundle-like
folders; it is not authenticated beyond the checks above. (3) The access token is protected by DPAPI
with a fixed entropy value, so other programs running as the same user can read it. (4) A
TLS-intercepting proxy would see the token.

## 8. Decision record: the client's credential (direct token or a small gateway)

Requirement (owner, 2026-10-19): one client, at most two users, **no GitHub account for them**, and no
personal GitHub token or other reusable privileged credential handed to the client or built into the
program. The signed manifest and the helper stay the same either way.

| | A. Direct fine-grained token (built) | B. Thin authenticating gateway |
|---|---|---|
| Client holds | a GitHub token for the owner's account (read-only *Contents* on one repository, expiry at most one year) | an activation key that only opens the gateway |
| New infrastructure | none | one small HTTPS function (any serverless platform) holding the GitHub token as a server-side secret |
| Revoke one user | regenerate the token (affects both users) | delete one key |
| If the credential leaks | read access to everything in the release repository | the ability to download releases, which are already private to the client; the GitHub token is not exposed |
| GitHub terms | handing a personal token to third parties should be checked | the client never sees a GitHub credential |
| Code | done and tested | missing locally: a configurable HTTPS base address in `config.py` (host fixed in the build) used instead of `api.github.com`, with the same three GET paths and the same ignore-everything-GitHub-says rules; not written, because the exact reply shape (stream or redirect) depends on the service that does not exist yet |
| Trust | the Ed25519 signature protects against a bad package in both; a compromised gateway can refuse or replay old signed releases but cannot install anything unsigned |
| Time to a real end-to-end test | the shortest | one more service to create |

**Recommendation.** Use A once, by the owner, to prove the real chain (private repository, signing
key, signed release, real download and install) because it needs no new service. Do not give the
client a token: for the client, build B. It is the simplest design that satisfies the requirement,
because the gateway only forwards three read-only requests and the program already refuses to believe
anything the transport says that the signature does not cover.

**Shortest approved sequence (nothing below has been done; each step needs the owner):**

1. Create a private GitHub repository for releases.
2. Generate the signing key offline: `python scripts\release\release_tool.py keygen --key <path>`
   (needs `openssl`; Git for Windows ships one); keep the private key offline.
3. Put `owner/name` and the printed public key into `src/offline_doc_tools/updater/config.py` and
   rebuild a *separate* "updates enabled" build; the v0.1 client build stays unconfigured.
4. Publish a test release from that private repository with `release_tool.py release`.
5. Test A from a second copy of the build with a short-lived token of the owner's own (revoke it after).
6. For the client: create the gateway and its secret, then write and test the base-address change
   (the missing code above), rebuild, deliver that build by hand once, hand out activation keys.

Until step 3 the shipped build can never update itself, and a build with placeholder values must not be
delivered as "update-ready".
