# Portable test-suite scope and exclusions

The `unittest discover -s tests -v` suite is intentionally different between
the private development tree and the public source artifacts. The source
archive contains every portable core product test. It excludes only the test
modules listed in `scripts.build_source_snapshot.EXCLUDE_TEST_PATHS`; the
selection is explicit rather than a broad `tests/` omission.

## Excluded module categories

| Category | Excluded modules | What the tests protect | Why not in the public archive | Portable core coverage |
| --- | --- | --- | --- | --- |
| Private release evidence | `test_current_release_audit`, `test_future_work_plan_audit`, `test_work_item_register`, `test_wp00_baseline_audit` | Historical release-state accounting, owner gates and work-package records | Their evidence subjects and approval records are deliberately not public payload | Version, package, CLI and payload boundaries remain covered by portable tests |
| Reference-distribution governance | `test_reference_asset_release`, `test_reference_distribution_inventory`, `test_reference_license_approval_template`, `test_reference_owner_approval`, `test_reference_public_catalog`, `test_reference_remote_audit` | Distribution and remote-publication authorization for external assets | Reference archives and authorization records are outside the core-only source scope | Core catalog parsing, local verification and fail-closed selection remain covered |
| Host/evidence operations | `test_dev5_runtime_coverage`, `test_resource_baseline_extractor`, `test_stage3_8_host_helpers`, `test_stage3_8_medecom_overnight_launcher` | Server launchers, host resource probes and historical evidence collection | They contain host-specific paths or call private operation helpers | Adapter, report, benchmark, evidence-source and installation tests remain portable |
| Private inventory | `test_license_inventory` | Full development-tree license inventory including excluded files | The public artifact needs artifact-bound payload audit, not private-tree inventory | `audit_conda_payload` and public prepublication tests cover archive boundaries |

The exact test IDs in each category are generated for every local candidate by
the RC review process. A test absent from both the worktree and the source
artifact, or an archive-only unexpected omission, is a release blocker.

The source archive runs without `.git`; no source-test assertion may require a
Git checkout. Skips must identify their exact unavailable optional subject and
must not turn a failed product check into a skip.
