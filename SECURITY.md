# Security policy

## Security maintainer

**João Encarnação** ([`@jencarnacao19`](https://github.com/jencarnacao19))
is the project owner and security maintainer of Sentinel CCTV.

The security maintainer reviews vulnerability reports, assesses their impact,
coordinates fixes and updates the project's security documentation. This is an
independently maintained project; response times depend on maintainer availability.

## Supported code

Security fixes target the latest code on the `main` branch. Older snapshots and
forks may need to upgrade. Sentinel is in early development; see
[project status](PROJECT_STATUS.md) for current limitations and
[security documentation](docs/security.md) for its threat model and protections.

## Report a vulnerability

Use GitHub's **Report a vulnerability** option on the repository's
[Security page](https://github.com/jencarnacao19/sentinel-cctv/security) when
private vulnerability reporting is available.

If that option is unavailable, open an
[issue](https://github.com/jencarnacao19/sentinel-cctv/issues/new) containing only
a request for a confidential security reporting channel. Wait for a private
channel before sharing the vulnerability details. Do not put an exploit or
sensitive information in a public issue, pull request or discussion.

A private report should include:

- The affected release or commit and component.
- The expected behaviour, observed behaviour and security impact.
- Minimal reproduction steps using dummy credentials and synthetic media.
- A suggested fix, if available.

Do not send passwords, API tokens, viewer keys, private environment files,
real camera stream URLs or private footage. Redact installation details from
logs and screenshots before sharing them.

The maintainer will assess the report, work on a fix where applicable and
coordinate disclosure with the reporter.
