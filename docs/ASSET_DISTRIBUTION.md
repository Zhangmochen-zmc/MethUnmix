# Static asset distribution contract (MethUnmix 2.0.0)

MethUnmix 2.0.0 uses a versioned, immutable JSON catalog over HTTPS. TUF is
deferred; the client does not claim signed catalog metadata, cryptographic
freshness, or protection from a compromised catalog host. HTTPS authenticates
the connection under the configured certificate trust store, while SHA256 and
byte length detect asset-object corruption or mismatch. These protections are
not equivalent to TUF.

Asset commands are explicit and offline-first:

```text
methunmix asset list
methunmix asset plan
methunmix asset verify
methunmix asset import ARCHIVE
methunmix asset audit --online --catalog-url https://HOST/catalogs/RELEASE/catalog.json
methunmix asset fetch ASSET_ID
methunmix asset gc --dry-run
```

`audit --online` accepts the exact versioned catalog URL. Its path must include
the catalog's `catalog_release`. A catalog sequence may only increase; the
same sequence cannot be rebound to different bytes. The audit stores each
catalog as a SHA256-addressed local snapshot and records the highest sequence
seen. This detects local rollback/rewrite attempts after a prior audit, but a
fresh installation has no cryptographic way to know whether a host is serving
the latest catalog. Do not describe the local sequence check as a signed
rollback defense.

Every `PUBLIC_DOWNLOADABLE` target must include an absolute HTTPS URL, a
content-addressed immutable object path containing its SHA256, exact byte
length, `immutable_object: true`, explicit `LICENSE_APPROVED` status and
license evidence, and a non-revoked status. Public assets must never be
overwritten in place. The current RC catalog is intentionally empty because
no external asset has yet received explicit redistribution approval. Assets
with unclear rights remain `USER_SUPPLIED_SUPPORTED` or `BLOCKED` and cannot
be fetched from a public catalog.

An empty public catalog is a valid release mode only when the catalog declares
`empty_catalog_policy` and the offline module-import contract passes. In that
mode MethUnmix does not claim that references, models, runtimes or tool code
are downloadable; users may import only compatible archives they are
authorized to use, and `BLOCKED` assets remain unavailable. License approval
for the Conda core and any bundled code remains a separate release gate.

Module manifests distinguish portable data from executable runtime payloads:
reference bundles and data-only tool/build modules use `platform: noarch`;
runtime/SIF and build-runtime modules use `platform: linux-x86_64`. Importing a
noarch module is supported on hosts where the Python control plane runs, while
executing any workflow remains Linux x86_64-only. Unknown architecture labels
and missing platform declarations are rejected rather than treated as portable.

Downloads are streamed to a unique, exclusively created same-filesystem
temporary file, bounded by the declared size, checked for HTTPS redirects,
exact byte length and SHA256, then published without overwriting an existing
content-addressed cache name. Existing matching regular files are reused;
symlinks, hard-linked cache entries, corrupt cached bytes, mismatched lengths,
and oversized responses are rejected. A catalog may declare at most two
additional HTTPS mirrors; every mirror must use the same content-addressed
SHA256 path and each response is independently checked. Mirrors are tried only
after a transport/incomplete-transfer failure; integrity or policy failures
fail closed. HTTPS downgrades and cross-host redirects are rejected. Archive
installation rejects archive symlinks (including symlinked parent paths),
hashes and extracts from the same open file descriptor, and checks that the
opened archive's identity, size and change timestamps did not change during
installation. A checksum sidecar is opened without following symlinks, must be
a regular UTF-8 file no larger than 4 KiB, and must contain a 64-hex SHA256.
Tar members are preflighted before extraction: only `module.json` and
`payload/` entries are accepted; paths must be canonical NFC POSIX paths and
portable across Windows, case-folding collisions and file/directory conflicts
are rejected, and symlinks, hard links and special files are not accepted.
Archives are bounded to 250,000 members and 128 GiB of expanded file content.
Staged file data and directories are flushed before publication on POSIX;
Windows flushes file data and uses the atomic rename publication step.
Import, catalog audit, asset fetch and mutating `asset gc` share one per-store
operation lock; concurrent writers fail closed rather than interleave
download, registration or cache deletion. Managed cache/catalog and
module-store directories reject symlink traversal. `asset gc` removes only
unreferenced SHA256-named cache objects; incomplete downloads, unknown files
and symlinks are left untouched.

The lock has local POSIX and Windows implementations. Only the local
filesystem has been exercised in this RC; NFS/shared-filesystem locking and
stale-lock recovery are unvalidated and best effort. Use a local filesystem
for writes; shared stores may be consumed read-only until their filesystem's
locking semantics are separately verified.

Normal runs, list/plan/verify/import, and local reference operations never
contact a server. Network access occurs only after an explicit `asset audit
--online` or `asset fetch` request. A revoked record in the locally audited
catalog blocks a fetch; the static-catalog route does not promise immediate
revocation propagation to disconnected users.

The Conda package never contains BAM, BED, PAT, CRAM, SIF, model weights,
reference archives or genome caches. Full TUF roles, production signing keys,
cryptographic revocation freshness and key-rotation procedures are deferred
to a later release; no production private key is generated or stored for 2.0.0.
