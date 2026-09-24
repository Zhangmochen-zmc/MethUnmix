# Deferred TUF metadata

MethUnmix 2.0.0 uses the static immutable catalog contract documented in
`docs/ASSET_DISTRIBUTION.md`; this directory contains no active trust root and
is not consulted by the asset manager. TUF roles, signing keys, signed
metadata, cryptographic freshness, and key-rotation procedures are deferred
to a later release. Do not create or store production private keys here.
