# Rollback and recovery

Never move a published Git tag or overwrite an immutable asset. A recipe-only
fix increments the Conda build number. An upstream code fix creates the next
MethUnmix patch version. A bad asset receives a new asset version and a new,
higher-sequence immutable catalog that marks the old target revoked. Conda packages normally remain available; severe package
errors require coordination with Bioconda maintainers for a broken label or
repodata patch. Store versioning, object lock, independent catalog/asset
backup, and a mirror are required before publication. The static-catalog
client only receives revocation updates when users explicitly audit a newer
catalog; instant revocation and cryptographic rollback resistance are not
claimed. TUF key backup is not a 2.0.0 requirement because TUF is deferred.
