# Security Policy

## Supported versions

Security fixes are applied to the current `main` branch and the version running
on the public Dikarya service. Older commits, forks, and independently modified
deployments may not receive fixes.

## Reporting a vulnerability

Please report suspected vulnerabilities privately by email to:

`alanrockefeller [at] gmail [dot] com`

Use a subject such as `Dikarya security report`. Do not open a public GitHub
issue, discussion, or pull request for an unpatched vulnerability.

Include enough information to reproduce and assess the problem when possible:

- the affected route, feature, file, or commit;
- the vulnerability type and expected impact;
- concise reproduction steps or a minimal proof of concept;
- whether the issue affects the hosted service, the source distribution, or
  both; and
- any mitigations you have already identified.

Do not include real user DNA sequences, session cookies, access tokens, API
keys, database contents, or other private data. Use synthetic test data and
redact credentials from logs and screenshots.

Reports will be investigated privately. Please allow time to confirm the issue,
prepare a fix, and coordinate disclosure before publishing details. Good-faith
research that avoids privacy violations, data destruction, service disruption,
and access beyond what is necessary to demonstrate the issue is appreciated.

For ordinary bugs, feature requests, and documentation problems that do not
have a security impact, use the repository's public issue tracker.
