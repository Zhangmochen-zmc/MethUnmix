# RC and production testing

The test suite is deliberately layered. Static capability validation covers
every declared-valid and declared-invalid registry row. A minimum execution
fixture runs each public logical tool once, including output schema, exit code,
manifest and no-network checks. Frozen READY baselines use approved tolerances;
RU remains RU and keeps its warning.

The production gate is run again after the final tag, final Conda artifact and
production metadata/assets exist. If production payload digests are not
byte-identical to staging, the complete compatibility gate is repeated.

No Slurm, CRAM, 118 GB BAM, or unbounded performance run is part of this
release. Every executable test has a timeout and independent output directory.

Container selection accepts `auto`, the `apptainer`/`singularity` names, or an
explicit absolute executable path whose basename identifies one of those
engines. Doctor records the selected executable, version and source and uses a
30-second `exec IMAGE.sif true` smoke when a local runtime image is installed.
Absence of a local SIF is reported as `NOT_PERFORMED`, not as a successful
container-runtime check. Unit tests mock the SIF invocation; actual engine/SIF
runtime evidence remains a separate local or external Linux acceptance item.
