# MethUnMix 1.0.0 fixed artifact — release text

The distribution version remains `1.0.0`; exact artifact identity is recorded
separately in the owner-reviewed release manifest. This text does not assert
that a tag, GitHub Release, PyPI/BioCode upload, Bioconda package, or deployment
has already occurred.

## Corrections

- Consume `runtime_module` and `logical_runtime_module` bindings consistently
  and revalidate selected runtime identities immediately before launch.
- Revalidate the selected SIF byte count and SHA256 at launch time.
- Reject unknown dependency kinds and identity drift.
- Replace a misleading missing-dependency warning with state that reflects the
  selected, verified local binding.
- Use the immutable candidate.6 public reference catalog in English and Chinese
  installation documentation.
- Preserve the lightweight core boundary: no reference archive, SIF/container,
  model, cache, input data, or separately licensed algorithm implementation is
  added.

## Same-version identity note

An older MethUnMix 1.0.0 direct-download wheel remains immutable at the URL and
SHA256 documented in the README. It is not byte-identical to the fixed artifact.
Users must install by exact URL/file and verify SHA256; the version string alone
cannot distinguish the two objects.

## Non-changes

This patch does not change reference bytes/schema, adapter algorithms, model
parameters, scientific thresholds, READY/RU state, truth data, or frozen
historical evidence. Workflow execution, output conformance, reproducibility,
and scientific validity remain separate conclusions.

Owner authorization is still required for every remote action. No pre-release
artifact is renamed to create this release.
