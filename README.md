# MethUnMix 1.0.0

MethUnMix is a lightweight, offline-first control plane for DNA-methylation
deconvolution. The core package validates inputs and external assets, explains
an execution plan, launches supported adapters, and records provenance. Large
references, containers/SIF files, models, caches, third-party algorithms, and
user data are deliberately not embedded in the wheel or sdist.

The Python distribution version is `1.0.0`. Release state and artifact identity
must be established from the signed or owner-reviewed release manifest, not
from the version string alone. A different, older MethUnMix 1.0.0 wheel is
already available at this immutable direct URL:

```text
https://download.cncb.ac.cn/methbank/MethUnMix/wheel/methunmix-1.0.0-py3-none-any.whl
bytes: 457732
sha256: cf15a70e53fa3ecae2d940b90b919a0643724d38b48098d2a1532dabdda16133
```

That URL identifies the old 457,732-byte wheel, not the fixed release artifact. A
direct file URL is not by itself evidence of a GitHub, PyPI, BioCode, or
Bioconda release. A public old-1.0.0 sdist URL has not been verified. Never
replace, rename, or silently serve different bytes at the old URL.

## Install an identity-locked 1.0.0 artifact

Obtain the exact wheel and its owner-reviewed release manifest, then verify the
published SHA256 before installation. Because an older wheel has the same
filename and version, do not rely on `pip install methunmix==1.0.0` alone:

```bash
python3 -m venv /absolute/path/to/methunmix-fixed-1.0.0-venv
/absolute/path/to/methunmix-fixed-1.0.0-venv/bin/python -m pip install \
  --no-deps /absolute/path/to/release/methunmix-1.0.0-py3-none-any.whl
/absolute/path/to/methunmix-fixed-1.0.0-venv/bin/methunmix --version
```

Expected version output is `MethUnMix 1.0.0`. Because both old and fixed
artifacts report that distribution version, verify the release manifest
SHA256 and installed execution-content identity as well. The sole public console entry
is `methunmix`; `demethflow_core` remains an internal compatibility import path
and there is no public `demethflow` executable.

## Public reference catalog

The immutable current public reference catalog is:

```text
https://download.cncb.ac.cn/methbank/MethUnMix/reference/1.0.0/catalog/methunmix-reference-1.0.0-owner-rights-public-safe-candidate.6/catalog.json
bytes: 528233
sha256: 7ef6600563a0c182f14593ce1236b95558d0a860dfde22d282364b0e0cd805f2
```

The frozen 1.0.0 installed parser resolved 33/33 unique selectors as
`PUBLIC_DOWNLOADABLE`, each with an exact content-addressed URL, byte count,
and archive SHA256. Historical evidence independently covered all 33 archive
bytes. The candidate.6 post-deployment acceptance newly downloaded,
imported, and verified **one representative archive**, not all 33 again. These
two evidence scopes must not be conflated.

The core does not automatically discover this external catalog. Supply the
exact HTTPS URL explicitly and keep the audited metadata directory:

```bash
METHUNMIX=/absolute/path/to/methunmix-fixed-1.0.0-venv/bin/methunmix
ASSET_HOME=/absolute/path/to/methunmix-assets
CATALOG_URL=https://download.cncb.ac.cn/methbank/MethUnMix/reference/1.0.0/catalog/methunmix-reference-1.0.0-owner-rights-public-safe-candidate.6/catalog.json
CATALOG_METADATA=/absolute/path/to/catalog-metadata

"$METHUNMIX" --home "$ASSET_HOME" asset audit --online \
  --catalog-url "$CATALOG_URL" --metadata-dir "$CATALOG_METADATA"

"$METHUNMIX" --home "$ASSET_HOME" reference available \
  --metadata-dir "$CATALOG_METADATA"

"$METHUNMIX" --home "$ASSET_HOME" reference fetch --install \
  --metadata-dir "$CATALOG_METADATA" builtin.brain.450k@1.0.0

"$METHUNMIX" --home "$ASSET_HOME" asset verify \
  reference.builtin.brain.450k@1.0.0 --json
```

Use full selectors such as `builtin.brain.450k@1.0.0`; do not use a bare ID or
invent a `latest` alias. A valid reference does not make an analysis runnable:
the selected tool may also require an exact external runtime, SIF, model, or
cache identity.

## Plan, diagnose, and run

The same selection fields feed `asset plan`, `plan`, `doctor`, and `run`. Array
inputs must not receive `--genome-build`; native WGBS routes require an exact
build and analysis contract. Start with a non-executing plan:

```bash
"$METHUNMIX" --home "$ASSET_HOME" plan \
  --scenario brain --platform 450k --input /absolute/path/to/beta.csv \
  --reference builtin.brain.450k@1.0.0 --tools Houseman \
  --runtime container --accelerator cpu --check-mode quick --json

"$METHUNMIX" --home "$ASSET_HOME" doctor \
  --scenario brain --platform 450k --input /absolute/path/to/beta.csv \
  --reference builtin.brain.450k@1.0.0 --tools Houseman \
  --runtime container --accelerator cpu --check-mode deep --json

"$METHUNMIX" --home "$ASSET_HOME" run \
  --scenario brain --platform 450k --input /absolute/path/to/beta.csv \
  --reference builtin.brain.450k@1.0.0 --tools Houseman \
  --runtime container --accelerator cpu \
  --outdir /new/absolute/path/to/run --workflow-timeout-seconds 600
```

The run example is illustrative and needs the matching installed reference and
external runtime. Parser acceptance is not evidence that a workflow or its
scientific result succeeded. `plan` and `doctor` do not accept run-only options
such as `--random-seed` or `--workflow-timeout-seconds`.

## Patch scope

Relative to the old frozen 1.0.0 bytes, this fixed release source contains the
qualified engineering control-plane corrections, including strict consumption of
`runtime_module`/`logical_runtime_module`, launch-time identity revalidation,
and correction of a misleading dependency warning. It also updates version,
catalog, installation, and licensing documentation. It does not add algorithm
implementations, alter reference bytes or schemas, modify model parameters or
scientific thresholds, or promote scientific states.

## Licence and distribution boundary

- MethUnMix core code is distributed under the repository MIT licence.
- Project reference data are offered under CC BY-NC 4.0 only where the
  applicable licensor has authority to grant that licence.
- Twenty-four published reference archives retain GPL adapter notices.
- Native-WGBS objects use object-specific layered notices and retained upstream
  terms; the catalog and notice files are authoritative for each object.
- Affiliation with an academic institution does not automatically establish a
  non-commercial use. A contact address is not an automatic commercial
  licence or permission grant.

Inspect the catalog licence fields and object notice before downloading or
redistributing any reference. Core installation grants no rights to external
third-party runtimes, tools, models, or input data.

## Documentation

- [English user guide](docs/PUBLIC_USER_GUIDE.md)
- [中文用户指南](docs/PUBLIC_USER_GUIDE_zh-CN.md)
- [Reference installation guide](docs/REFERENCE_INSTALLATION_GUIDE.md)
- [Reference 安装指南](docs/REFERENCE_INSTALLATION_GUIDE_zh-CN.md)
- [Security policy](SECURITY.md)
- [Citation metadata](CITATION.cff)

Every run should preserve the software version and content identity, full
reference selector and digest, runtime/SIF identity, input identity, parameters
and seed, receipt, manifest, report, and canonical output checksums. Execution
success and scientific validity remain separate conclusions.
