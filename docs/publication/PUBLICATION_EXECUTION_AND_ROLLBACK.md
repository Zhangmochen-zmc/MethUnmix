# Publication execution and rollback — draft

Each numbered step is a separate authorization boundary.

| Step | Prerequisite / exact input | Authorized action | Success evidence | Failure stop / new authorization |
| --- | --- | --- | --- | --- |
| 1 | Owner approves scope and license disposition | Record decision against exact file set | Signed/recorded owner decision | Stop all publication work |
| 2 | Owner chooses public version/channel | Change authoritative version in a new candidate | Reviewed version diff | Revert candidate change; no tag |
| 3 | Clean approved commit | Build wheel, sdist, deterministic snapshot; lock hashes | Local build/install/payload records | Stop; produce corrected candidate |
| 4 | Exact file list and hashes | Review public source/docs/NOTICE/claims | Reviewer approval | Do not publish artifacts |
| 5 | Explicit owner release authorization | Create tag and publish approved artifacts | Published URLs and release hashes | Stop promotion; retain immutable evidence |
| 6 | Public artifacts available | Independent clean installation from public URL | Download/install identity record | Correct with a new version; do not overwrite an artifact |
| 7 | Approved public source URL/SHA | Submit downstream/Bioconda change | PR and recipe identity | Stop before downstream upload/merge |
| 8 | Official CI and trial authorization | Review official CI and controlled feedback | CI links and sanitized feedback | Fix in a new candidate/version as appropriate |
| 9 | All relevant engineering, rights, and science gates | Owner evaluates stable/scientific claims separately | Explicit owner decision | Do not infer promotion |

Rollback means stop subsequent promotion, preserve immutable released history,
record the defect, and prepare a new corrected version.  It does not mean
deleting evidence, silently replacing an artifact under the same version, or
rewriting prior scientific results.
