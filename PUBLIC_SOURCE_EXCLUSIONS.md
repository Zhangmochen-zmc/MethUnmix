# Public source exclusions

This independent source tree is a reviewed file set, not a copy of the 2,896
file internal product tree or its 462-commit history.

Excluded by design:

- internal approval packages, evidence registries and historical run logs;
- model outputs, scientific results and large test results;
- credentials, tokens, machine-specific binding and locator files;
- reference archives, SIF/container images, model weights and genome caches;
- user inputs, truth data and private server paths;
- separately licensed third-party algorithm/vendor source trees;
- generated build products and local virtual environments;
- legacy `demethflow` console entry points.

The Python import name `demethflow_core` and established serialized schema
names remain compatibility identities. The sole public console command is
`methunmix`.

The source set retains workflow templates, small test fixtures, LICENSE,
NOTICE and attribution needed to build and use the core. The candidate catalog
has no active URL and remains `USER_SUPPLIED_SUPPORTED`; it is included only
for review and offline validation.
