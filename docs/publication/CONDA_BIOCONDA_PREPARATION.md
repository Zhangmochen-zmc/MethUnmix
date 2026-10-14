# Conda/Bioconda preparation — draft, not a submission

The repository keeps two different materials separate:

1. a locally executable recipe used for internal package qualification; and
2. a downstream recipe draft whose source URL/SHA and maintainer list remain
   null until those identities are selected and authorized.

Before a public recipe is submitted, owner must approve the exact released
source archive, target account and PR action. Then fill only these fields from the
released artifact: stable URL, filename, SHA256, version, license/notice
metadata, and entry-point tests.  A new version starts at build number `0`.
Do not retrofit the frozen RC2 recipe or change it for later development.

The repository carries a deliberately non-submittable skeleton at
`bioconda/meta.yaml.pending`. It contains explicit nulls rather than a local
path, fabricated URL or fabricated identity. It declares MIT for the core
only; the separately distributed non-commercial references are not package
payload and do not alter the core license.

Official Bioconda CI, mulled tests, package upload/BioContainer generation,
and public installation are downstream steps.  A local Conda build or the
internal local-package SHA cannot substitute for any of them.
