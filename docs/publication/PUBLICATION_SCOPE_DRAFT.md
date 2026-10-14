# Public-core publication scope — owner-review draft

Status: `REFERENCE_LICENSE_AND_SOFTWARE_TARGETS_RECORDED_PUBLIC_ACTIONS_NOT_AUTHORIZED`.
This document is a file-scope proposal, not authorization to publish, tag,
upload, push, contact anyone, or change scientific status.

## Proposed scope

| Class | Proposed treatment | Exact boundary | Owner decision needed? |
| --- | --- | --- | --- |
| A. Core source | Reviewed fixed 1.0.0 source | integrate the reviewed tree into the existing GitHub history without rewriting history | Tag, push and release remain unauthorized |
| B. Wheel/sdist | Built from the final reviewed tree | core Python code, workflow templates, descriptors, public docs, LICENSE, NOTICE | Exact artifact and public action approval |
| C. Documentation and micro synthetic example | Public draft | README, public guides, release notes, parser/no-model examples only | Public action remains pending |
| D. Conda recipe | Draft only | a future downstream recipe with public source URL/SHA, build number 0 | Yes: target/channel and recipe approval |
| E. Reference catalog/archive | Separate from core | candidate.6 records 33 versioned selectors and immutable public URLs | Object notices and retained upstream terms remain authoritative; no archive is repackaged here |
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
