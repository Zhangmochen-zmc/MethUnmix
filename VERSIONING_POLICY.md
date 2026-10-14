# MethUnMix versioning policy

Core software uses SemVer. This independent public line starts at `1.0.0`;
earlier local `2.x` candidates and their evidence keep their original identities
and are not rewritten or rebound to 1.0.0. Conda build numbers are independent
integers and must not change scientific content.

Asset/reference versions, catalog schema, catalog release sequence, manifest
schema, runtime API versions and scientific-evidence identities are independent
namespaces. In particular, the embedded catalog's `core_version` records the
catalog snapshot producer; it is not required to equal the current core version.
Compatibility is enforced by the catalog schema and per-target declarations.

Compatibility ranges use inclusive lower and exclusive upper bounds. Missing
required fields or an unknown major schema fail closed. The current static
catalog is unsigned; a SHA256 snapshot is not a signature or proof of publisher
authenticity. No production signing key is created by this candidate work.
