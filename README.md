# MethUnmix

MethUnmix is the tentative product name for an offline-first Nextflow workflow
being prepared as a successor to DeMethFlow. Final name, repository continuity,
and public-release approval remain pending.

This repository contains the lightweight Conda/package control plane. It does
**not** contain the 46 GB aggregate offline distribution, SIF containers,
reference archives, model weights, genome caches, BAM/PAT/BED fixtures, or
other large assets. Those are installed explicitly through the static-catalog
asset store or imported from a user-owned offline archive. The initial catalog
is intentionally empty until redistribution rights are approved.

## Platform boundary

The core package and offline asset/reference-management commands can be used
on platforms supported by Python. Starting reference-construction or
deconvolution workflows is explicitly limited to Linux x86_64 and fails early
on other hosts. A working Nextflow/OpenJDK installation and an explicit local
container engine are required. CRAM is out of scope for 2.0.0. Slurm is not
part of the release scope.

Reference bundles and data-only modules are architecture-independent and may
be imported on supported Python hosts. SIF/runtime modules are tagged
`linux-x86_64` and cannot be imported on other architectures. This asset
management boundary does not make workflow execution cross-platform.

## Commands

```bash
methunmix --version
methunmix doctor --help
methunmix asset list
methunmix asset import /path/to/module.tar.gz
# `--home` is an absolute store path; it may precede the subcommand or follow
# the selected top-level command.
methunmix --home /data/my-methunmix asset list
methunmix asset --home /data/my-methunmix list
```

The store path precedence is `--home`, `METHUNMIX_HOME`/legacy
`DEMETHFLOW_HOME`, `XDG_DATA_HOME/methunmix`, then
`~/.local/share/methunmix`. A relative `--home` or configured home is rejected;
blank environment values count as unset. If both home variables are non-empty
and resolve to different locations, store-using commands fail with a
configuration error instead of silently choosing one. An explicit `--home`
overrides both variables for that command without mutating the caller's
environment. `--help` and `--version` do not inspect the store configuration.
The Conda prefix is never used as a writable asset store.

`run` never downloads assets automatically. Network access is only performed
by an explicit `methunmix asset audit --online --catalog-url URL` or
`asset fetch` command. The first-release catalog uses HTTPS and per-object
SHA256/length checks; signed TUF metadata is deferred and no TUF security claim
is made.
The historical `demethflow` command remains a compatibility alias throughout
the 2.x series.

## Status

This checkout is a Conda release-candidate implementation. It is not a claim
that every scientific capability is already `READY`; scientific status,
distribution status, Conda compatibility, and CPU/GPU status are recorded as
independent fields.
