# Controlled trial guide — public-core draft

Status: `DRAFT; NOT AN INVITATION OR ARTIFACT URL`.  This guide preserves the
previous no-model trial boundary while removing server-local package paths.

## Before beginning

The trial coordinator supplies an approved artifact filename/SHA256 and a
separate execution-content identity.  Install in a fresh environment outside
any source checkout, clear `PYTHONPATH`, and verify the public entry point.
The coordinator must not substitute an internal local Conda build or an
unpublished development wheel for a public artifact.

## Permitted no-model exercise

```bash
methunmix --version
methunmix adapter validate --json
methunmix adapter list --json
```

These are no-model, no-workflow checks.  They do not verify external runtime
availability, a SIF, a reference archive, a model, a deconvolution result, or
scientific accuracy.

## Asset and workflow boundary

Asset-management commands need an approved archive/catalog/binding under the
trial’s separate authorization.  A `COMPLETE` asset receipt says only that the
selected bounded installation plan is satisfied.  It does not authorize a
workflow or establish a scientific result.

Do not invoke `run`, `replay-execute --execute`, or `retry-failed-tool
--execute` during a no-model trial.  A route-specific real workflow must have
its own owner authorization for input/reference/runtime identities, resource
limit, expected output contract, and feedback capture.

## Reporting

Use [`ISSUE_REPORT_TEMPLATE.md`](ISSUE_REPORT_TEMPLATE.md).  Include sanitized
command argv and raw exit status.  Exclude private data, credentials, absolute
personal paths, and external assets.  A failed trial is an installation/CLI
observation unless a separately authorized workflow says otherwise.
