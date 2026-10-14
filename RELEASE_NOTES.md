# MethUnMix 1.0.0 fixed artifact release notes

This fixed 1.0.0 artifact retains the public distribution version `1.0.0` but
has different bytes from the older wheel already served at
`https://download.cncb.ac.cn/methbank/MethUnMix/wheel/methunmix-1.0.0-py3-none-any.whl`.
The old object is not overwritten. Users must select and verify the fixed
artifact through the owner-reviewed release manifest and SHA256.

The fixed source includes accumulated control-plane corrections, notably:

- strict consumption of `runtime_module` and `logical_runtime_module` local
  dependency bindings;
- launch-time SIF size and SHA256 revalidation;
- fail-closed handling of unknown dependency kinds and identity drift;
- correction of a misleading logical-dependency warning;
- current candidate.6 public reference catalog instructions and clarified
  core/reference/runtime licence boundaries.

This release does not alter reference scientific payloads, truth data,
baselines, READY/RU policy, model parameters, or scientific acceptance
criteria. Large references, SIF files, models, caches, third-party runtimes,
credentials, and user inputs remain outside the core wheel and sdist.
