# Version and channel plan — fixed 1.0.0 artifact

The Python distribution version remains exactly `1.0.0`. Artifact byte
identity, source commit, and installed execution-content identity are separate
release-manifest fields because an older, different 1.0.0 wheel already exists.

The old 1.0.0 wheel remains directly downloadable at the CNCB URL recorded in
the README and must remain byte immutable. The fixed artifact uses a different,
content-addressed publication path. A direct file URL and a formal platform
release are different facts.

| Destination | Prepared transaction | Remote gate |
| --- | --- | --- |
| GitHub | exact public integration commit, tag target, release text and attachment digests | owner-authorized push, tag and Release |
| PyPI | validated wheel/sdist set and upload checks | owner account upload; reject if version or filename becomes occupied |
| BioCode | correction plan for the existing entry and release | authenticated metadata correction and attachment action |
| Bioconda | recipe bound to the validated PEP 517 sdist | stable anonymous sdist URL, owner PR authorization and official CI |

No step in this document authorizes a tag, push, release, upload, BioCode
submission, website deployment, Bioconda PR, or remote reference mutation.
