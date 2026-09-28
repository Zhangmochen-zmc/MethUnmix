# MethUnmix release gates

The only publishable state is `PUBLIC_RELEASE_READY`. It requires:

```text
NAME_AND_IDENTITY_READY
BASELINE_READY
PACKAGE_READY
ASSET_DISTRIBUTION_READY
INSTALLATION_E2E_READY
SCIENTIFIC_COMPATIBILITY_READY
PRODUCTION_ARTIFACT_VERIFIED
LICENSE_APPROVED
SBOM_READY
GPU_CLAIMS_MATCH_EVIDENCE
ROLLBACK_DRILL_PASSED
```

The staging candidate must pass the complete RC qualification. After the
formal tag, production metadata/assets and the final Conda package must be
verified again. A staging pass cannot promote production automatically.

The current RC status is tracked gate-by-gate in the private release record;
the aggregate remains `METHUNMIX_CONDA_RC_BLOCKED`. The scientific-compatibility sub-gates are
independent: the baseline is frozen, local unit tests pass, seven Houseman
routes have repeatability observations, but execution coverage is only 7/199,
Houseman numerical acceptance is undefined, and no authoritative prior
Houseman output is available for those exact selectors. These partial results
do not close the scientific, regression, or WGBS gates.

The gate breakdown separately records repository, baseline freeze, unit tests,
execution coverage, repeatability, scientific acceptance policy, regression
compatibility, WGBS, licensing, SBOM, Conda clean install, external
Bioconda/mulled CI, GPU runtime, production artifact verification and rollback.
Local CPU work must not be blocked merely because an external GPU runner or
Bioconda CI is unavailable; conversely, an external-only gate remains pending
until its own evidence exists.

For 2.0.0, asset distribution uses an immutable static catalog over HTTPS,
with per-object SHA256/length validation and a local monotonic sequence check.
TUF signatures and production keys are deferred and are not a current gate.
The public catalog must remain empty until each downloadable asset has an
explicit redistribution approval; USER_SUPPLIED_SUPPORTED assets are not
published by MethUnmix. An explicitly declared empty catalog may satisfy the
asset-catalog implementation/distribution gate when the safe offline import
contract passes. This does not waive the separate core/vendor license gate or
make any blocked capability available.

`SBOM_READY` requires an artifact-digest-bound CycloneDX SBOM for the exact
source/wheel/Conda build, resolved Conda dependencies, each distributed SIF,
and every public reference/model/cache target. The current core-only SBOM
candidate only describes staged source/wheel artifacts and bundled vendor code; its
`PARTIAL_RC_CORE_ONLY` scope is not a release pass.

`run` never performs network updates. Only explicit `asset audit --online`
and `asset fetch` commands access the network. Revocation in the static model
applies when a client audits a newer catalog; disconnected users do not receive
instant revocation updates. HTTPS plus SHA256 does not protect a fresh install
from a compromised catalog host and must not be described as TUF-equivalent.

No formal tag, public upload, Bioconda PR or production signing-key operation
may occur until the project owner explicitly authorizes that release action.
