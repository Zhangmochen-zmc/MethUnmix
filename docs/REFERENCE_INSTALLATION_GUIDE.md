# MethUnMix reference installation guide — catalog 1.0.0

This guide applies to the external candidate.6 catalog and the unpublished
MethUnMix fixed 1.0.0 core candidate `fixed-1.0.0-candidate.1-20261014`.
It is distinct from the old public 1.0.0 wheel. Core and reference versions are independent:
upgrading the core does not silently replace a reference, and publishing a
reference does not change scientific status.

## Authoritative catalog identity

```text
URL: https://download.cncb.ac.cn/methbank/MethUnMix/reference/1.0.0/catalog/methunmix-reference-1.0.0-owner-rights-public-safe-candidate.6/catalog.json
bytes: 528233
SHA256: 7ef6600563a0c182f14593ce1236b95558d0a860dfde22d282364b0e0cd805f2
selectors: 33 unique, 33 PUBLIC_DOWNLOADABLE
declared archive bytes: 18461347873
```

Each archive URL is content addressed as
`.../objects/sha256/<archive-sha256>/<archive-name>.tar.gz`. Never infer an
archive identity from its basename. The catalog's selector, byte size, SHA256,
licence fields, and notice links form the contract.

The candidate.6 post-deployment closure independently read all 11 newly
created small catalog/notice/evidence objects. Historical evidence covers the
33 archive bytes, while the closure newly repeated installed-client
fetch/import/verify for one representative archive. It did not download all 33
again.

## 1. Install and identify the client

Verify the candidate wheel against its evidence manifest before installation:

```bash
python3 -m venv /absolute/path/to/methunmix-venv
/absolute/path/to/methunmix-venv/bin/python -m pip install \
  --no-deps /absolute/path/to/fixed-candidate/methunmix-1.0.0-py3-none-any.whl
/absolute/path/to/methunmix-venv/bin/methunmix --version
```

## 2. Audit the catalog explicitly

The client has no implicit online catalog discovery. Choose new writable paths
and pass the exact URL:

```bash
METHUNMIX=/absolute/path/to/methunmix-venv/bin/methunmix
ASSET_HOME=/absolute/path/to/methunmix-assets
METADATA=/absolute/path/to/methunmix-catalog-metadata
CATALOG_URL=https://download.cncb.ac.cn/methbank/MethUnMix/reference/1.0.0/catalog/methunmix-reference-1.0.0-owner-rights-public-safe-candidate.6/catalog.json

"$METHUNMIX" --home "$ASSET_HOME" asset audit --online \
  --catalog-url "$CATALOG_URL" --metadata-dir "$METADATA"
"$METHUNMIX" --home "$ASSET_HOME" reference available \
  --metadata-dir "$METADATA"
```

`asset audit --online` is the explicit network step. Preserve its metadata and
receipt. Confirm the returned catalog size and SHA256 before any fetch.

## 3. Plan first

Planning is selection-specific. This example does not install or run a model:

```bash
"$METHUNMIX" --home "$ASSET_HOME" asset plan \
  --metadata-dir "$METADATA" --scenario brain --platform 450k \
  --input-type array --reference builtin.brain.450k@1.0.0 \
  --tools Houseman --accelerator cpu --mode decon --summary
```

The plan may remain blocked after reference installation because external
runtime/SIF/model/cache dependencies are separate. `PUBLIC_DOWNLOADABLE`
describes the reference archive, not the whole execution environment.

## 4. Fetch, import, and verify one exact selector

```bash
"$METHUNMIX" --home "$ASSET_HOME" reference fetch --install \
  --metadata-dir "$METADATA" builtin.brain.450k@1.0.0

"$METHUNMIX" --home "$ASSET_HOME" reference validate \
  --checksums builtin.brain.450k@1.0.0

"$METHUNMIX" --home "$ASSET_HOME" asset verify \
  reference.builtin.brain.450k@1.0.0 --json
```

Interrupted downloads are staged and integrity checked; an incomplete or
wrong-digest object must not become installed. Repeat installation of the same
verified identity is idempotent. A different version is an upgrade decision,
not an implicit replacement.

For an archive already obtained through an approved offline transfer, use the
ordinary reference installation path instead of fabricating a catalog URL:

```bash
"$METHUNMIX" --home "$ASSET_HOME" reference install \
  /absolute/path/to/exact-reference.tar.gz
```

## 5. Run readiness checks

After installing all exact dependencies required by the route, run `plan` and
`doctor` with the same selection. Array routes omit `--genome-build`; native
WGBS routes declare the exact hg19/hg38 contract. Missing or drifted assets
fail closed. A successful reference verify does not imply workflow or
scientific success.

## Licence boundary

MethUnMix core is MIT licensed. Project reference data are offered under CC
BY-NC 4.0 only where the applicable licensor has authority. Twenty-four
archives retain GPL adapter notices. Native-WGBS objects use object-specific
layered notices and retained upstream terms. Read the exact catalog record and
notice before download or redistribution. Academic affiliation alone does not
establish non-commercial use, and a contact address is not a permission grant.

## Evidence boundary

Record the core version/content identity, catalog URL/digest, full selector,
archive size/digest, installed manifest digest, asset home, runtime/SIF
identity, parameters, and run receipt separately. Reference availability does
not promote READY/RU, validate a model, or prove biological accuracy.
