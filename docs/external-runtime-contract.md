# External algorithm runtime contract

The MethUnmix core Conda package contains the control plane, adapters,
canonical output checks and Nextflow orchestration.  CelFiE, CelFEER, MetDecode,
PRMeth and UXM implementations are not redistributed by the core package.

Install those implementations through an independently licensed runtime tree
and pass its root explicitly:

```text
external-runtime/
├── methunmix-external-runtime.json
├── CelFiE/
├── CelFEER/
├── MetDecode/
├── PRMeth/
└── UXM/
```

The manifest uses schema `methunmix-external-runtime-v1` and must declare, for
each selected tool, a safe relative `path`, implementation `identity`,
`version`, license/distribution disclosure, and `required_files`.  MethUnmix
does not download, clone, modify or execute an implementation while checking
the contract.  Missing paths, files, or metadata fail closed with an explicit
`EXTERNAL_RUNTIME_*` diagnostic before Nextflow is launched.

The same contract is used by `doctor`, `run`, the native WGBS workflow and the
array PRMeth route.  A `run_manifest.json` records the resolved paths and
declared identity/version/license; scientific status and reference/truth
digests remain independent and are never promoted by runtime availability.
