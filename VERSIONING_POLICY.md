# MethUnmix versioning policy

Core software uses SemVer. Conda build numbers are independent integers and
must not change scientific content. Asset/reference versions, catalog schema,
catalog release sequence, manifest schema, and runtime API versions are all
independent namespaces.

Compatibility ranges use inclusive lower and exclusive upper bounds. Missing
required fields or an unknown major schema fail closed. Unknown optional fields
are preserved by catalog snapshotting and ignored by compatible readers. The
2.0.0 catalog is unsigned; a SHA256 snapshot is not a signature or proof of
publisher authenticity.

The embedded catalog is part of the core source payload. Changing it requires
a MethUnmix patch release; a Conda build-number increment alone may only
correct recipe metadata or packaging mechanics. No TUF trust root is bundled or
used in 2.0.0; TUF is deferred and no production signing key is created.
