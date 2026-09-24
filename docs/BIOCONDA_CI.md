# Bioconda channel validation

The dependency-free local checks in `scripts/audit_conda_recipe.py` validate
identity, entry points, dependency bounds, URL and source checksum. They do not
replace the channel's build system.

Once `bioconda-utils` is installed in a dedicated CI environment, run from a
clean clone:

```bash
bioconda-utils lint --git-range master...HEAD
bioconda-utils build --docker --mulled-build-and-test --git-range master...HEAD
```

The recipe tests are intentionally installation-only:

```text
methunmix --version
methunmix --help
methunmix doctor --help
methunmix asset --help
methunmix asset list
demethflow --version
```

They must not download assets, require a source checkout, access the network,
or run Nextflow/scientific tools. Full workflow and scientific compatibility
tests belong to the upstream MethUnmix release CI. Record the exact
`bioconda-utils`, conda-build, channel and Docker image versions in the
production artifact evidence.
