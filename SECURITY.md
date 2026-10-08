# Security Policy

VisionOps processes camera video and stores security-sensitive events, so we
take reports seriously. This document is the public disclosure channel; the
in-depth design and threat model live in
[`docs/SECURITY_PRIVACY.md`](docs/SECURITY_PRIVACY.md).

## Reporting a vulnerability

**Do not open a public issue for a vulnerability.** Report it privately to:

- Email: `security@visionops.example` (replace with the operating
  organisation's security mailbox before release)

Please include:

- the affected version or commit, and how it is deployed (native/systemd,
  Docker, edge);
- a description of the issue and its impact;
- reproduction steps or a proof of concept;
- any suggested mitigation.

If you need to send sensitive material, encrypt it with the key published by
the maintainers, or ask for an out-of-band channel first.

## Response targets

| Stage | Target |
| --- | --- |
| Acknowledge receipt | 3 business days |
| Triage + severity assessment | 7 business days |
| Fix or mitigation for critical/high | 30 days from triage |
| Coordinated public disclosure | 90 days, or by agreement |

We credit reporters on request and will keep you informed if a fix takes
longer than the target above.

## Scope

In scope:

- the API, vision pipeline and web console in this repository;
- authentication, authorisation and tenant isolation;
- camera-credential handling and secret storage;
- path/URL handling (SSRF, traversal), rate limiting and resource caps;
- the shipped deployment artefacts (`infra/**`).

Out of scope:

- findings that require a already-compromised host or root access;
- denial of service from unauthenticated networks against a host the operator
  exposed without a reverse proxy (see `docs/DEPLOYMENT.md` hardening);
- third-party dependencies with no exploitable path in VisionOps (report
  upstream, but tell us too).

## Supported versions

Security fixes are backported to the latest released minor version only. The
project is pre-1.0; pin a tag and track releases.

## What we already do

The authoritative checklist is `docs/SECURITY_PRIVACY.md` §10. In short: API
keys are hashed with a per-key salt and compared in constant time; camera
credentials are encrypted at rest and never returned by the API; every domain
row carries a non-null `organization_id`; file paths are containment-checked
against the media root; source URLs are scheme-allow-listed; and rate, body,
page and WebSocket limits are enforced and tested.
