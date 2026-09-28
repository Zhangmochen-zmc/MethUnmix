# Release RACI

| Activity | Responsible | Accountable | Consulted | Evidence |
|---|---|---|---|---|
| Product/CLI name audit | Maintainer | Release owner | Legal/community | private name-review record |
| Core package build | Maintainer | Release owner | Bioconda reviewer | wheel, source archive, recipe |
| Static catalog and immutable object publication | Asset custodian | Release owner | Security reviewer | versioned catalog, SHA256 and URL audit |
| License and redistribution review | Asset custodian | Legal/PI | Tool authors | license matrix, notices |
| Baseline and capability matrix freeze | Science lead | Release owner | Tool maintainers | baseline and matrix JSON |
| Conda lint/build tests | Packaging maintainer | Release owner | Bioconda reviewer | CI logs |
| Static catalog sequence/revocation drill | Security reviewer | Release owner | Asset custodian | rollback evidence; TUF deferred |
| Public release/announcement | Release owner | Project owner | Bioconda/community | tag, DOI, release notes |

Names and dates are deliberately blank until the project assigns owners. A release must not claim these gates are complete without named sign-off.
