# Public-core publication scope — owner-review draft

Status: `REFERENCE_LICENSE_AND_SOFTWARE_TARGETS_RECORDED_PUBLIC_ACTIONS_NOT_AUTHORIZED`.
This document is a file-scope proposal, not authorization to publish, tag,
upload, push, contact anyone, or change scientific status.

## Proposed scope

| Class | Proposed treatment | Exact boundary | Owner decision needed? |
| --- | --- | --- | --- |
| A. Core source | Local reviewed candidate | integrate the reviewed tree into the existing GitHub history on a new local branch | Tag, push and release remain unauthorized |
| B. Wheel/sdist | Rebuild required after author metadata update | core Python code, workflow templates, descriptors, public docs, LICENSE, NOTICE | Exact new artifact and public action approval |
| C. Documentation and micro synthetic example | Candidate | README, public guides, release notes, parser/no-model examples only | Public action remains pending |
| D. Conda recipe | Draft only | a future downstream recipe with public source URL/SHA, build number 0 | Yes: target/channel and recipe approval |
| E. Reference catalog/archive | Separate from core | 30 derived rows approved for distribution; `CC-BY-NC-4.0` selected for rights-held contributions; all URLs null | Object-level rights/upstream review, reference rights holder, hosting target and public action remain pending |
| F. Runtime/model/cache | Excluded; interface only | external descriptors and `USER_SUPPLIED_SUPPORTED` contracts only | Separate runtime/model/cache rights decision |
| G. Historical raw evidence | Excluded | server-only indexes may be cited privately, never packaged as public evidence | No automatic publication |

The core boundary does **not** include any SIF, reference archive, model,
genome cache, raw or truth input, external algorithm implementation, local
binding locator, credential, or server runtime.  `USER_SUPPLIED_SUPPORTED`
means the core can check and consume a user-supplied identity; it does not
grant download or redistribution rights.

## Public source archive versus full history

The proposed public source archive is the deterministic allowlist produced by
`scripts/build_source_snapshot.py`.  It has a narrow public-document list and
excludes private evidence generators, host launchers, vendor directories, and
externalized algorithms.  It is suitable for review only after the staged
archive itself passes the prepublication checker.

The existing public GitHub repository is the intended software destination.
Its published history and existing tags remain in place. The reviewed source
tree is introduced as a new commit descended from the current public default
branch; old history is not force-replaced. This local preparation does not
authorize a push, tag or release.

## Frozen baseline retained

The following internal identities remain evidence references only and are not
proposed public release names: product commit
`d2e5e6cd53155dd6e7fc0e09d764842fd4408b87`, internal version
`2.1.0.dev14+provenance1`, wheel
`6c45366a1d13b97ef2b56afcb9e69c6b765e52c617f4d2a4a2719bd4248ed086`,
sdist `bf97ee83e36a9cd5e96b868c70bbcc71070223719c173597511dacc461d2ab67`,
and execution-content identity
`86ac046162f1d77c4cc6e09b491e9c82a65788ec105cb8935d181a01d7015247`.
