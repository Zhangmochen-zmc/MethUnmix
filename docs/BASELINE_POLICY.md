# Baseline and tolerance policy

Baseline generation and tolerance approval precede all RC and production
comparisons. Each baseline records the software tag, commit, runtime/reference
and input digests, truth digest, random seed, threads, CPU/GPU, BLAS/OpenMP
environment, post-processing version, expected outputs, metrics and approved
tool-specific tolerances.

Results must never be used to retroactively choose a tolerance. A Conda result
outside the approved tolerance suspends inheritance of the old READY state and
requires a compatibility audit.

The owner-confirmed science policy for this release is deliberately
tool-specific: Pearson >= 0.80 is a READY gate only for existing MethylBERT
routes. It is not a universal threshold for the 21 tools. For other routes,
report observed metrics without inventing new hard gates. Repeatability and
CPU/GPU agreement use tolerances extracted from the corresponding historical
tool evidence and frozen before any new comparison. Thresholds already present
for non-MethylBERT routes in historical manifests remain per-capability
candidates; they are not automatically promoted to Conda release gates.
Do not claim external generalization where no independent cohort is available.

For on-demand user-built array references, held-out simulations still record
MAE, Pearson, per-cell error, output validity and repeat difference. Observed
accuracy/repeatability values are report-only unless a matching tool/platform
policy is explicitly approved and frozen in the code-owned
`BUILD_VALIDATION_POLICIES` registry. Thresholds emitted by a container QC
file are not trusted as approval. With technically valid outputs but no
approved policy, the generated capability remains `RELEASED_UNVALIDATED`; it
is not promoted to `READY` and is not quarantined solely for low Pearson/MAE.
Broken output contracts, failed structural/marker gates, and failed explicitly
approved tool-specific policies remain blocking conditions. The existing
MethylCIBERSORT marker-stability QC remains separate and tool-specific.

The owner-confirmed simulated input root is recorded in the local release
evidence. The `850K` fixture directory is normalized to EPIC while preserving
its original label in the inventory. Fixture data and machine-specific
absolute paths are not part of the public source or Conda payload.

Fixture joins are contract-aware. Native 450K/EPIC inputs join only to their
native array contracts; EPIC-to-450K uses an EPIC input fixture with its
450K-reference contract; WGBS-derived array routes join only to a WGBS fixture
with the same biological scenario and genome build. A reference target's
platform is not treated as the submitted input platform. The local review
candidate includes only current READY/RU selectors with an available,
sample-matched fixture and complete input/truth digests. It records
QUARANTINED/NOT_AVAILABLE/NOT_ADAPTED and missing-fixture cases as exclusions,
not passes or failures. The candidate is not frozen until the owner approves
its exact selector/policy scope; package installation cannot promote a
scientific state.
