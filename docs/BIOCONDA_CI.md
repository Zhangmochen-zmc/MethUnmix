# Bioconda channel validation

Recipe-specific static source-binding checks are maintained in the private
release workspace. They do not replace the channel's build system and are not
part of the public source distribution.

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
