# Reference deposit plan — review draft

This plan proposes metadata and grouping only. It does not upload or alter a
reference archive and does not change any scientific state. CC BY-NC 4.0 is
selected for rights-held project contributions, while whole-archive scope is
still constrained by object-level upstream terms and rights-holder review.

## Frozen source identities

The proposed records use the current candidate catalog's 33 exact selectors,
byte lengths and archive SHA256 values. Every selector is `@1.0.0`; public
basenames contain no version. The catalog identity remains separate from core
1.0.0, and all source URLs remain null. A same-named archive is never a
substitute for the catalog's exact digest and length.

## Hosting remains undecided

Do not infer a reference host from the selected GitHub and BioCode software
targets. If a suitable reference platform is later selected, keep software and
reference data in separate records:

1. one software record for the exact MethUnMix 1.0.0 source release;
2. six dataset records grouped by biological scenario: brain, breast,
   epithelial, fetal19, immune12 and immune6;
3. one small catalog/collection record linking the six versioned dataset
   records, their future identifiers, the catalog JSON, manifest and
   `SHA256SUMS`, only after a platform is chosen.

Scenario grouping keeps each record independently versionable and avoids
replacing unrelated archives when one scenario changes. The frozen inventory
is exactly 18,507,507,226 bytes across 33 archives. No remote reference
platform or record layout is currently selected; scenario grouping remains a
local proposal. BioCode is not assumed to accept all reference archives.

Each scenario record should contain only its exact archives plus:

- `README.md` with scope and non-scientific-status disclaimer;
- `MANIFEST.json` with selector, length, archive SHA256,
  reference-manifest digest and license-evidence binding;
- `SHA256SUMS`;
- applicable license/NOTICE and citation material.

## Publication TODOs

- Author order, affiliation and contact are recorded; ORCID values are omitted
  because none were supplied.
- Owner confirms the reference legal rights holder; the selected standard
  license is `CC-BY-NC-4.0` for contributions the licensor may license.
- Confirm whether reference-specific citation and upstream-license text is
  complete for every included component.
- Stage each exact archive into a new reviewed directory; do not reuse a
  same-named file without digest and length verification.
- Reserve or publish no DOI until platform and per-action authorization exist.
- After records exist, replace catalog URL placeholders with immutable record
URLs and perform independent download/hash checks before enabling a catalog.
