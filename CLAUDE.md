# Security review before pushing

This project has had three prior rounds of post-push findings from HANCORE's
manual review and the `security-guidance` plugin's automated scan on GitHub
issue #14811 (Omarchy marketplace submission) - each one caught something
real (cross-peer token reuse, a secret-file TOCTOU, a fingerprint/identity
binding bug, a token exposed via CLI argv) but each round cost a maintainer
review cycle that a local check could have caught first.

Before committing/pushing any change that touches auth, tokens, grants,
known_hosts, secret-file writes, or peer/coordinator trust
(`modelmanager/auth.py`, `modelmanager/grants.py`, `modelmanager/known_hosts.py`,
`modelmanager/secret_files.py`, `modelmanager/cli.py`'s auth/grant/hosts
commands, `modelmanager/coordinator/routes.py`'s auth checks, or docs
describing any of that): run the `security-review` skill against the diff
and resolve anything it finds, *before* asking the user for the push
go-ahead. Report the review's outcome (clean, or what was fixed) alongside
the usual diff summary, so the push decision already accounts for it instead
of waiting on HANCORE or the bot to find it after the fact.

This doesn't replace HANCORE's review or the automated scan - it's a cheaper
first pass to stop obviously-fixable issues from costing a maintainer cycle.
