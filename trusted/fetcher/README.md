# trusted/fetcher

Trusted Stage 5 read-only web mediation.

- `app.py` exposes the trusted internal fetch route and health check.
- `policy.py` enforces URL normalization, allowlist checks, redirect re-validation, IP blocking, and text-only response rules.
- This service is the only Stage 5 component attached to the non-internal `egress_net`.

The bridge calls this service over `trusted_net`. The untrusted agent never reaches it directly.
