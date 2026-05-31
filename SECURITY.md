# Security Policy

`plex-watch-sync` mirrors Plex watched-state across configured accounts and
holds long-lived Plex tokens for those accounts. The intended deployment
shape is an internal-only Docker network with no external exposure, but
issues that could leak tokens, bypass the configured user/label gating, or
let an attacker on the local network manipulate watched state for accounts
they aren't configured for are taken seriously. This is a small,
community-maintained project with best-effort support.

## Supported versions

Fixes land on the latest released minor and the `:latest` / newest release
tags. Older minors are not back-patched.

| Version | Supported          |
| ------- | ------------------ |
| 0.5.x   | :white_check_mark: |
| < 0.5   | :x:                |

Pin a specific release (e.g. `:0.5.3`, ideally by digest) for reproducible
deployments; track `:0.5` or `:latest` to pick up fixes on the next pull.

## Reporting a vulnerability

**Do not open a public issue for security problems.**

Use GitHub's private vulnerability reporting:
**Security → Report a vulnerability** on
<https://github.com/tikibozo/plex-watch-sync/security/advisories/new>.

Please include affected version/tag or digest, a description and impact, and
reproduction steps. Expect an acknowledgement within ~7 days (best effort);
fixes ship as a patch release with an advisory once a fix is available.

## What's in place

Every push, pull request, and a weekly schedule run `gitleaks` for committed
secrets; published images are blocked on HIGH/CRITICAL findings by a
pre-publish Trivy scan, and a scheduled scan of `:latest` catches CVEs that
emerge after release. Dependency and base-image updates are automated via
Renovate. This reduces but does not eliminate risk — independent reports are
still valued.
