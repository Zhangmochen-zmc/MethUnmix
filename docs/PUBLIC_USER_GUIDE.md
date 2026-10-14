# MethUnMix user guide — fixed 1.0.0 artifact

> Artifact identity is authoritative. This guide applies to the fixed 1.0.0
> artifact named by an owner-reviewed release manifest, not to the different
> old 1.0.0 wheel at the existing direct URL. The version string alone cannot
> distinguish them. This guide is not a scientific-validation result or a
> change to READY/RU status.

MethUnMix is an offline-first control plane for DNA-methylation
deconvolution.  It selects a route, checks user-provided inputs and external
assets, invokes an eligible workflow, and records provenance.  The core does
not bundle reference archives, SIF/container images, model weights, genome
caches, raw input data, or third-party algorithm implementations.

The old same-version object is retained unchanged for provenance:

```text
https://download.cncb.ac.cn/methbank/MethUnMix/wheel/methunmix-1.0.0-py3-none-any.whl
bytes: 457732
sha256: cf15a70e53fa3ecae2d940b90b919a0643724d38b48098d2a1532dabdda16133
```

That URL is not the fixed artifact. The fixed wheel and sdist are selected by
their distinct immutable URLs and SHA256 values in the external release
manifest. This avoids a self-referential artifact whose own documentation
would contain its final archive digest.

## Install the core

Install only the exact release artifact after comparing its SHA256 with the
owner-reviewed release manifest. It must not replace the bytes at the existing
public 1.0.0 URL. Do not assume that an index, mirror, or pip cache returns the
fixed bytes merely because it reports version `1.0.0`.

```bash
python3 -m venv /absolute/path/to/methunmix-fixed-1.0.0-venv
/absolute/path/to/methunmix-fixed-1.0.0-venv/bin/python -m pip install \
  --no-deps /absolute/path/to/release/methunmix-1.0.0-py3-none-any.whl
/absolute/path/to/methunmix-fixed-1.0.0-venv/bin/methunmix --version
```

`methunmix` is the only public command installed by this artifact. Historical
`demethflow_core` import and schema names remain internal compatibility
identities and must not be presented as a second CLI.

Record the artifact SHA256 from the release manifest and the installed
`execution_content_identity` in every issue report.  The first identifies the
archive bytes; the second identifies installed core code and bundled execution
resources.  They are not interchangeable.

## A no-model quick experience

The following reads the bundled declarative adapter registry only.  It does
not download assets, start a container, invoke Nextflow, or run a
deconvolution method.

```bash
methunmix adapter validate --json
methunmix adapter list --json
```

`status: PASS` from these commands means that the packaged descriptor schema
is valid.  It is not a workflow, accuracy, runtime, or scientific claim.

## Audit and install the current public reference catalog

The current immutable catalog is:

```text
https://download.cncb.ac.cn/methbank/MethUnMix/reference/1.0.0/catalog/methunmix-reference-1.0.0-owner-rights-public-safe-candidate.6/catalog.json
bytes: 528233
sha256: 7ef6600563a0c182f14593ce1236b95558d0a860dfde22d282364b0e0cd805f2
```

It describes 33/33 unique selectors as `PUBLIC_DOWNLOADABLE`. Historical
evidence covers all 33 archive bytes; the candidate.6 closure newly repeated
installed-client fetch/import/verify for one representative archive. It did
not redownload all 33. The core does not discover this URL automatically.

```bash
METHUNMIX=/absolute/path/to/methunmix-fixed-1.0.0-venv/bin/methunmix
ASSET_HOME=/absolute/path/to/methunmix-assets
METADATA=/absolute/path/to/catalog-metadata
CATALOG_URL=https://download.cncb.ac.cn/methbank/MethUnMix/reference/1.0.0/catalog/methunmix-reference-1.0.0-owner-rights-public-safe-candidate.6/catalog.json

"$METHUNMIX" --home "$ASSET_HOME" asset audit --online \
  --catalog-url "$CATALOG_URL" --metadata-dir "$METADATA"
"$METHUNMIX" --home "$ASSET_HOME" reference available \
  --metadata-dir "$METADATA"
"$METHUNMIX" --home "$ASSET_HOME" reference fetch --install \
  --metadata-dir "$METADATA" builtin.brain.450k@1.0.0
"$METHUNMIX" --home "$ASSET_HOME" asset verify \
  reference.builtin.brain.450k@1.0.0 --json
```

Always use a full versioned selector. A verified reference does not replace a
required runtime, SIF, model, cache, or exact local dependency binding.

## Before a real analysis

You must supply compatible inputs and externally managed dependencies.  A
real request declares a scenario, input platform, route, exact reference
selector, tool(s), profile/device, seed where applicable, and a new output
directory.

| Command | Purpose | May start a workflow? |
| --- | --- | --- |
| `asset plan` / `asset import` / `asset verify` | Resolve, import, and verify declared external assets | No workflow in the qualified asset-management path |
| `plan` | Explain the selected route and execution readiness | No workflow, but a complete configuration may perform a bounded runtime/container smoke check |
| `doctor` | Validate current inputs, references, bindings, and host | No workflow, but a complete configuration may perform a bounded runtime/container smoke check |
| `run` | Create and execute a new workflow attempt | Yes |
| `report` | Produce a derived report from an existing run | No |
| `benchmark` | Compute descriptive user-truth metrics | No workflow |
| `replay-plan` | Prepare a display-only replay plan | No |
| `replay-execute --execute` | Explicitly execute one strict replay in a new directory | Yes |
| `retry-failed-tool --execute` | Explicitly start one fresh single-tool retry after a terminal failure | Yes |

Do not use placeholder paths as a real configuration.  Read `--help`, create
a new output directory, set a complete timeout, and run `plan`/`doctor` for
your exact route before `run`.

## Platforms and routes

Array-native inputs (450K or EPIC) have `genome_build: not_applicable` in
their presentation metadata.  Native WGBS inputs require the declared hg19 or
hg38 contract.  WGBS-derived array projection and EPIC-to-450K projection are
separate routes and require explicit opt-in; they must not be inferred from a
target platform alone.

Five external runtime integrations are `USER_SUPPLIED_SUPPORTED`.  A matching
reference is not a replacement for its runtime, model, cache, or local
binding.  Missing or digest-incompatible dependencies fail closed; the core
does not fall back to a checkout and does not automatically download them.

## Results, replay, and retry

`run_manifest.json`, receipts, leases, canonical-output hashes, and derived
reports record different facts.  A workflow exit of zero, output-contract
conformance, and scientific evidence are separate.  Anonymous components
(for example, MeDeCom components) are not named cell types and must not be
scored as such without a validated mapping.

`replay-plan` is display-only.  `replay-execute` consumes structured fields,
revalidates current software/input/reference/runtime identities, creates a
new output directory, and never executes a historical shell command, enables
Nextflow resume, or reuses partial output.  Cross-version replay is not
implemented.  `retry-failed-tool` is instead a fresh restart of one terminal
failed/timed-out tool; it is not replay, Nextflow resume, or step-level
recovery.

The R/MeDeCom diagnostic wrapper records external stderr, stage events, and
metadata.  A record such as `42 warnings` without individual warning text is
an unknown-warning-content result, not evidence that warnings were harmless
or fully captured.

## Troubleshooting and support

Start with `--help`, a machine-readable plan/doctor result, the receipt,
lease, `run_manifest.json`, `result_summary.json`, and the derived report.
Do not edit historical records.  Report only sanitized logs and relative
paths.  Do not submit credentials, private input data, tokens, or unapproved
reference/runtime contents.

Use the issue template in
[`docs/publication/ISSUE_REPORT_TEMPLATE.md`](publication/ISSUE_REPORT_TEMPLATE.md).
For a controlled trial, see
[`docs/publication/CONTROLLED_TRIAL_INVITATION_DRAFT.md`](publication/CONTROLLED_TRIAL_INVITATION_DRAFT.md).

## Removal and data retention

Uninstalling the Python distribution does not remove user-owned input data,
asset homes, runtime directories, or run results.  Review the asset store and
result locations you selected before deleting anything.  Never delete an
asset home merely to clear an error without first checking which runs and
shared dependencies use it.

## Citation and attribution

Use [`CITATION.cff`](../CITATION.cff) for the core citation.  `LICENSE` and
`NOTICE` apply to the distributed core as scoped there.  External references,
runtimes, models, caches, and upstream algorithms retain their own terms;
they are neither included nor relicensed by the core package.

The core authors, in release order, are Mochen Zhang, Xiangyu Yu and Yaoke
Wei; the contact is Mochen Zhang (`zhangmochen@big.ac.cn`). All three declare
the affiliation “Beijing Institute of Genomics, Chinese Academy of Sciences
(China National Center for Bioinformation)”; no ORCID has been supplied. The
core remains MIT licensed and permits commercial use. Reference material is
separate. The owner selected CC BY-NC 4.0 for contributions the applicable
licensor is entitled to license: compliant non-commercial research use,
sharing, and adaptation do not require a separate project application, while
attribution, license notice, and change indication remain required.
Twenty-four archives retain GPL adapter notices, while native-WGBS objects use
object-specific layered notices and retained upstream terms. Third-party
material keeps its own terms. Commercial use requires prior
permission from the party entitled to grant it; contact Mochen Zhang
(`zhangmochen@big.ac.cn`) to request routing. Contact is not permission, and
academic or university affiliation alone does not determine whether a use is
NonCommercial. Consult each catalog record and object notice; a contact address
is not an automatic permission grant.
