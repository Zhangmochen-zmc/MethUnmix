# MethUnMix 1.0.0 release notes — fixed local candidate

Status: `NOT_RELEASED`. The distribution version is `1.0.0`, while the local
candidate is independently identified as `fixed-1.0.0-candidate.1-20261014`;
no tag, GitHub Release, PyPI/BioCode upload, Bioconda package, or release date
is asserted.

## Patch contents

- Consume `runtime_module` and `logical_runtime_module` bindings consistently
  and revalidate selected runtime identities immediately before launch.
- Replace a misleading missing-dependency warning with state that reflects the
  selected, verified local binding.
- Update version sources, citation metadata, README and English/Chinese user
  documentation to the current immutable candidate.6 public catalog.
- Preserve the lightweight core boundary: no reference archive, SIF/container,
  model, cache, input data, or separately licensed algorithm implementation is
  added.

## Public-state clarification

- An older MethUnMix 1.0.0 is available as an exact direct-download wheel whose
  size and SHA256 are documented in the README. It is not byte-identical to
  this fixed candidate and must not be overwritten or silently replaced.
- The candidate.6 external reference catalog has 33/33 `PUBLIC_DOWNLOADABLE`
  selector contracts. Historical evidence covers 33 archive byte identities;
  the candidate.6 closure repeated client fetch/import/verify for 1/33.
- Direct URLs do not by themselves prove formal GitHub, PyPI, BioCode, or
  Bioconda release status.

## Non-changes

This patch does not change reference bytes/schema, adapter algorithms, model
parameters, scientific thresholds, READY/RU state, truth data, or frozen
historical evidence. Workflow execution, output conformance, reproducibility,
and scientific validity remain separate conclusions.

Owner authorization is still required for the exact commit, artifacts, URL,
conflict treatment, and every remote action. The fixed candidate was rebuilt
from exact versioned source; no rc artifact was renamed.
