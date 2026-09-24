# Catalog integrity, limitations and deferred TUF

## MethUnmix 2.0.0 first-release model

The first release uses a static immutable catalog fetched from an exact,
versioned HTTPS URL. Each downloadable object has a SHA256 and exact byte
length in that catalog, and the asset URL is content-addressed. The client
checks the HTTPS scheme (including redirects), catalog schema, target
redistribution approval, asset size and SHA256. A local monotonic sequence
record detects a lower sequence or changed catalog bytes at the same sequence
after that installation has audited a catalog.

This model intentionally has no signature verification or cryptographic
freshness. In particular, it cannot protect a new installation from a
compromised hosting account or prove that a served catalog is the newest one.
An immutable URL is a publishing/hosting invariant, not a cryptographic
property the client can independently establish. The static catalog and
HTTPS/SHA256 design is the approved minimal first-release scope; do not create
production signing keys for it.

## Revocation and rollback

Catalog snapshots are retained under sequence-and-digest names. A catalog
sequence cannot be silently rewritten locally, and lower sequence numbers are
rejected after a higher sequence has been audited. A target marked revoked is
refused for new fetches. Because metadata is unsigned and users may remain
offline, revocation is only as current as their last explicit audit or core
catalog update. Never overwrite an asset object or installed module; publish a
new asset version and a higher-sequence catalog instead.

Rollback means deliberately selecting a previously audited immutable catalog
snapshot or restoring a previous package, while preserving the record of the
currently highest audited sequence. The client must not silently lower that
sequence. If an operator needs to roll back catalog selection, this requires
an explicit recovery procedure and must not be represented as a normal online
catalog update.

## TUF after 2.0.0

A future TUF implementation would add an offline root, delegated targets,
snapshot and timestamp roles, threshold signatures, expiry, key rotation, and
cryptographic rollback/freshness rules. It requires a separate security
design, owner authorization, key-custody process, implementation and tests.
Those steps are not a 2.0.0 RC blocker and are not implemented by the static
catalog client. No private key is present in this repository or package.
