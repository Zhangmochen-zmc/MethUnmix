# Post-release maintenance

Patch releases may fix packaging, compatibility, security, or metadata errors without silently changing an asset digest. Any reference/model/cache change receives a new immutable asset version and a new catalog snapshot. Keep at least one prior package/catalog snapshot for rollback.

At every release, rerun the payload audit, clean Conda installation smoke tests, doctor checks, static-catalog schema/digest checks, and declared capability-matrix audit. Reverify production artifacts after staging; a staging pass is not production evidence. Revoke an asset by publishing a higher-sequence immutable catalog and a replacement object; do not overwrite the prior catalog/object. Revocation reaches users only when they explicitly audit or install an updated catalog/core package.

Scientific status is never promoted merely because an installation or runtime test passed. GPU claims are device-specific: H100 evidence does not establish support for other GPU models. No telemetry or network access is permitted during normal analysis.
