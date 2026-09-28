# MethUnmix Conda/Bioconda 2.0.0 release plan

This is the executable release checklist. `[x]` means the listed local
implementation or audit passed within its declared scope; it is not legal,
scientific, owner, external-CI, or production approval. Human sign-offs and
production operations remain unchecked until their named owner and evidence
are recorded. A successful Python import or Conda solve never promotes
scientific status.

## Gate 0 — identity and scope

- [x] Record the owner-confirmed RC naming direction: tentative product name
  `MethUnmix`, Bioconda package and CLI `methunmix`, legacy `demethflow` alias
  throughout 2.x, and internal `demethflow_core` retained in 2.0. DOI/PyPI are
  deferred and are not RC gates. Automated registry searching remains a
  preliminary signal, not legal clearance or name reservation.
- [x] Record the former `DeMethFlow` name and the compatibility alias without
  claiming ownership of unrelated marks or domains.
- [x] Freeze scope: core is noarch Python; complete workflow is Linux x86_64;
  CRAM, Slurm, and the 46 GB aggregate are out of scope for this release.
- [ ] Obtain final maintainer/legal sign-off and identify the actual standalone
  source repository before stable public release or repository migration.

## Gate 1 — reproducible baseline

- [ ] Freeze immutable source snapshot, Python/Nextflow/OpenJDK/container versions,
  core API, reference schema and runtime API.
- [ ] Freeze every asset selector, version, SHA256, byte length, genome build,
  cell-type order, random seed, input digest, tolerance and scientific scope.
- [ ] Store the owner-approved, digest-bound baseline in the private release
  record; public documentation describes the policy but does not distribute
  host-specific release evidence. Templates are not evidence.

## Gate 2 — package and compatibility

- [ ] Build the exact source archive and wheel from a clean checkout. Verify
  deterministic source hash and update the recipe hash only after the archive
  is final (the recipe is intentionally excluded from the source archive to
  avoid a self-referential checksum).
- [x] Normalize wheel ZIP member timestamps with `scripts/normalize_wheel.py`
  and compare two normalized builds in the same locked builder environment;
  raw setuptools wheel timestamps otherwise encode build time and are not a
  byte-reproducible artifact.
- [x] Run `scripts/audit_conda_payload.py`; no SIF/BAM/CRAM/PAT/BED/CSI/BAI,
  references, models, caches or runtime bundles may occur in the package.
- [x] Keep `demethflow` as a 2.x compatibility entry point and test both CLIs.
- [ ] Run `bioconda-utils lint` and the Bioconda Docker/mulled build test in CI;
  local wheel checks do not replace those channel checks.

## Gate 3 — assets and trust

- [ ] Complete the owner-reviewed per-asset license/redistribution matrix and
  full SBOM set for the resolved Conda environment, each public SIF, and every
  downloadable reference/model/cache asset.
- [x] Automatically refresh the 1,883-row license/source inventory and produce
  a CycloneDX core-artifact SBOM candidate bound to exact staged source/wheel
  SHA256 values. The core-only BOM is explicitly partial; it does not satisfy
  the full SBOM gate while external asset and resolved Conda dependency BOMs
  are absent.
- [x] Implement the first-release static catalog contract with a monotonic
  sequence, immutable release identity, HTTPS content-addressed target schema,
  exact lengths and SHA256 validation. TUF/signatures are deferred; no
  production keys are created. Local contract, rollback and digest-mismatch
  regressions pass.
- [ ] Configure and independently verify immutable public HTTPS catalog and
  asset URLs after approved redistribution targets and hosting ownership exist.
  The current embedded catalog intentionally has zero public targets.
- [ ] Keep the public catalog empty until each target has explicit
  redistribution approval; otherwise classify it `USER_SUPPLIED_SUPPORTED` or
  `BLOCKED` and keep it out of public URLs.
- [ ] Verify the exact staged static catalog and artifacts. A staging pass is
  not production evidence; production URLs and bytes are verified only after
  release authorization.
- [x] Exercise rejection of lower catalog sequences, same-sequence rewrites,
  revoked targets and incorrect asset digest/length.

## Gate 4 — capability and execution

- [x] Freeze the 21-tool registry and generate the valid/invalid matrix with
  stable platform-inclusive capability IDs.
- [x] During private release qualification, audit every invalid capability
  reason before process start and require every valid row to declare an
  explicit route family, input contract, build and device policy.
- [x] Each capability candidate records required reference artifacts,
  device-specific runtime/data modules, scientific/distribution/execution
  state, CPU/GPU Conda state, public reproducibility status and a stable
  evidence locator. Pending external Conda/GPU CI and license states are
  explicit; a matrix pass does not promote those gates.
- [ ] Run short Linux x86_64 smoke fixtures for each declared route and tool,
  including `doctor`, output schema, manifest, no-network and timeout checks.
- [x] Record container engine name, absolute executable path, version and
  discovery source (`explicit`, Conda or system) in doctor/run-manifest output;
  accept an explicit absolute Apptainer/Singularity path and put its directory
  on the Nextflow child `PATH`.
- [ ] On a host with a local runtime module, doctor executes one bounded
  `apptainer|singularity exec IMAGE.sif true` smoke (30-second timeout). If no
  SIF is installed, report `NOT_PERFORMED`; do not count that as engine/SIF
  runtime validation.
- [x] Regression-test that workflow execution is rejected before side effects
  on non-Linux/non-x86_64 hosts while package and offline asset-management
  commands remain available; the current fast suite covers this guard.
- [ ] Validate CPU/GPU profiles separately. H100 evidence applies only to H100;
  no broader GPU claim is allowed. RU remains RU with a visible warning;
  QUARANTINED and NOT_AVAILABLE remain blocked.

## Gate 5 — production promotion

- [ ] Rebuild from the tagged source, publish the exact archive, and verify its
  SHA256/length against the staged recipe and SBOM.
- [ ] Reinstall in a clean offline environment with no source checkout or
  network. Import/verify an asset module and run the declared smoke fixtures.
- [ ] Update `RELEASE_STATUS.md`, release notes and documentation with the
  complete status matrix, validation scope and known limitations.
- [ ] Require named sign-off for identity, licenses, packaging and science.
  TUF is not a 2.0.0 gate. Only after explicit release authorization may a
  formal tag, public upload or Bioconda PR be created.

## Release outcomes

`METHUNMIX_CONDA_RC_BLOCKED` is the correct state while any gate is pending.
`PUBLIC_RELEASE_READY` is allowed only after all gates and the second
production verification pass. A package can be Conda-installable while some
tools are RU; those are separate, explicitly disclosed states.
