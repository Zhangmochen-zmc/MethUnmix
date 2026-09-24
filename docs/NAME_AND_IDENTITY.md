# Name and identity gate

The current owner decision is tentative: product `MethUnmix`, Conda package
and formal CLI `methunmix`, legacy CLI `demethflow` throughout 2.x, and the
internal `demethflow_core` import path retained in 2.0. DOI and PyPI are not
RC blockers and will be decided during formal 2.0.0 preparation.

The automated preliminary search results and limitations are recorded in
`evidence/name_identity_audit.json` and `evidence/cli_collision_audit.txt`.
No exact indexed match was found, but search-engine results do not reserve a
registry name or prove trademark/domain ownership. Final owner/legal clearance
is still required before stable public release.

The owner confirmed that this is the first independent Git repository for the
offline tool; no previous standalone offline-tool Git history exists to
migrate. The web-server repository is a separate consumer and is not the source
repository. The MethUnmix Conda RC checkout is the audited canonical source
root: package control plane, packaged Nextflow workflows/adapters,
tests, Bioconda draft and release metadata. Large references, models, caches,
runtime images and generated `build/`/`dist*/` trees are not source payload.

A local `main` repository has been initialized at this root with no remote.
Files are staged for owner review. No initial commit was made because neither
local nor global Git author/committer identity is configured; no identity was
invented. Machine-generated `evidence/` files include host-local absolute
paths, and vendor redistribution/attribution review remains open; both must be
reviewed before any public push. No formal tag, push, public upload, production
key or Bioconda PR is authorized or performed. Stable public name/trademark and
repository owner/slug review remain pending.
