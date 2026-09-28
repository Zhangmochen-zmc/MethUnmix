# Name and identity gate

The current owner decision is tentative: product `MethUnmix`, Conda package
and formal CLI `methunmix`, legacy CLI `demethflow` throughout 2.x, and the
internal `demethflow_core` import path retained in 2.0. DOI and PyPI are not
RC blockers and will be decided during formal 2.0.0 preparation.

The automated preliminary search results and limitations are retained in the
private name-review record. No exact indexed match was found, but search-engine
results do not reserve a registry name or prove trademark/domain ownership.
Final owner/legal clearance is still required before stable public release.

The owner confirmed that this is the first independent Git repository for the
offline tool; no previous standalone offline-tool Git history exists to
migrate. The web-server repository is a separate consumer and is not the source
repository. The MethUnmix Conda RC checkout is the audited canonical source
root: package control plane, packaged Nextflow workflows/adapters,
tests, Bioconda draft and release metadata. Large references, models, caches,
runtime images and generated `build/`/`dist*/` trees are not source payload.

A local `main` repository is maintained at this root with no public remote.
Private release records may contain host-local paths and are deliberately
excluded from public source snapshots. Vendor redistribution/attribution review
remains open and must be resolved before public asset distribution. No formal
tag, push, public upload, production key or Bioconda PR is authorized or
performed. Stable public name/trademark and repository owner/slug review remain
pending.
