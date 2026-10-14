# Adapter contribution template

This template is for a proposed adapter.  It is not a plugin loader and does
not authorize downloading, bundling, or running a third-party implementation.
Only the reviewed built-in adapter set is executable in this candidate.

## Descriptor skeleton

```json
{
  "schema": "methunmix-adapter-descriptor-v1",
  "adapter_id": "example-tool-v1",
  "adapter_api_version": "1",
  "tool": "ExampleTool",
  "workflow_family": "array-example",
  "supported_analysis_contracts": ["array_450k_cpg"],
  "input_representations": ["array_matrix"],
  "reference_requirement": "required",
  "runtime_requirement": "USER_SUPPLIED_SUPPORTED",
  "sample_constraint": "single_or_multi_sample",
  "supported_profiles": ["cpu"],
  "output_contracts": ["cell_proportions_v1"],
  "operations": [
    "validate_input", "prepare_input", "execute", "parse_output",
    "validate_output", "collect_provenance"
  ],
  "provenance_fields": ["input_identity", "reference_identity", "runtime_identity", "parameters", "output_identity"],
  "parameter_semantics": ["random_seed:workflow_global"],
  "model_requirements": [],
  "cacheable_steps": [],
  "recovery_semantics": "no_adapter_output_cache",
  "error_classes": ["INPUT_CONTRACT", "RUNTIME_CONTRACT", "REFERENCE_CONTRACT", "OUTPUT_CONTRACT"],
  "scientific_disclosure_source": "reference_manifest_release_disclosure"
}
```

## Required contract tests

- Reject an incompatible input contract before preparing an execution command.
- Reject an unavailable runtime/profile/reference identity before execution.
- Verify exact argument construction, including seed and any model parameter.
- Verify valid output, malformed output, missing output, and anonymous-axis
  behavior without assigning cell-type names implicitly.
- Verify the recorded provenance includes adapter ID/version, input/reference/
  runtime identities, parameters, random-seed semantics, output contract and
  cache/recovery limitation.
- Verify no output cache, partial output, or Nextflow resume is inferred unless
  an explicitly reviewed contract adds it.

## Runtime, reference, provenance and license declaration

Record each independently; do not substitute a URL or tool name for identity.

```text
runtime: asset ID/version/digest, acquisition status, license/source evidence
reference: selector/content digest/schema compatibility
model/cache: immutable ID or explicit USER_SUPPLIED_SUPPORTED limitation
provenance: software execution-content identity, adapter ID, command parameters,
            seed semantics, input/output identity, execution and contract status
science: exact scope of any evidence; absence of evidence stays unreviewed
```

An adapter that can execute but lacks evidence remains at its existing declared
scientific status: execution never promotes scientific status. Descriptor review and this template are engineering
artifacts, not scientific approval or public distribution authorization; this
template does not enable dynamic plugin discovery.
