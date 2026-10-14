# Version and channel plan — fixed 1.0.0 local candidate

The Python distribution version is `1.0.0`, while
`fixed-1.0.0-candidate.1-20261014` is the unpublished local qualification
identity derived from the qualified 1.0.1rc1 source. Local construction
reserves nothing on a public service.

The old frozen 1.0.0 wheel remains directly downloadable at the CNCB URL
recorded in the README and must remain byte immutable. The fixed candidate has
the same distribution version but different bytes and installed content. A
direct file URL and a formal versioned platform release are different facts.
The public old-1.0.0 sdist remains `NOT_VERIFIED`.

| Destination | Candidate transaction state | Remaining gate |
| --- | --- | --- |
| GitHub | exact commit/tag/release draft to be bound after qualification | live history/fast-forward audit and owner authorization for tag, push and Release |
| PyPI | metadata and rendering preflight only | live version-occupancy check, owner upload authorization and post-upload verification |
| BioCode | form/attachment draft only | authenticated account, reviewed fields and explicit submission authorization |
| Bioconda | recipe draft only; no reference/SIF/model payload | immutable upstream sdist URL/SHA, local recipe checks, owner PR authorization and official CI |

## Same-version conflict options

- **X:** retain the old direct URL as a labelled historical candidate and, only
  after explicit authorization, publish the fixed 1.0.0 at a new immutable URL
  with both SHA256 values shown side by side. Package-manager caches still make
  same-name/same-version distribution hazardous, so platform uploads require
  an explicit conflict-free transaction.
- **Y (rollback):** keep the old 1.0.0 identity immutable and assign the fixed
  contents a successor distribution version before public publication.

No step in this document authorizes a tag, push, release, upload, BioCode
submission, website deployment, Bioconda PR, or remote reference mutation.
