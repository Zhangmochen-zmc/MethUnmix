# Security policy

Report suspected security issues privately to Mochen Zhang at
`zhangmochen@big.ac.cn`. Do not include credentials, private methylation data,
tokens, unpublished reference data, or unrestricted host logs in a public
issue.

MethUnMix verifies declared content identities but cannot make an untrusted
runtime, SIF, model, cache, reference, or input safe. Obtain external assets
from their documented source, compare the exact byte count and SHA256, inspect
the applicable licence, and keep local dependency bindings outside untrusted
write locations. A successful `plan`, `doctor`, import, or checksum check is
not a malware assessment or a scientific-validation result.

The supported security-fix line is the exact artifact set identified in the
current owner-reviewed release manifest. The distribution version alone is
insufficient because an older, different MethUnMix 1.0.0 wheel exists. Include
the wheel SHA256 and installed execution-content identity in reports.
