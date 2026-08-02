# ADR-0009 — TLS terminates on the origin, and the tunnel is rejected

Status: accepted
Date: 2026-08-02
Context: issue #11, the public deployment

## Context

The demo has to be reachable at a public URL over TLS, and the deployment has to be reproducible
from the repository — `git clone && docker compose up` with a `.env` should produce the running
system, or ADR-0001's portability characteristic is aspiration rather than architecture.

Three ways to get a certificate in front of a container on an Oracle Ampere VM were considered.

## Decision

**Caddy, in a container, on the same host, terminating TLS at the origin.**

`caddy:2.11-alpine`, official multi-arch so `linux/arm64` is a first-class target, about twenty
megabytes resident. ACME is automatic, the HTTP-to-HTTPS redirect is automatic, renewal needs no
cron and no hook, and the configuration is a six-directive `Caddyfile` in git with the domain
substituted from the environment.

## Why not a Cloudflare Tunnel

It is free, it needs no open inbound port, and it is **rejected** — on two grounds, the second of
which is the one that matters.

**It is not reproducible from the repository.** A tunnel runs on a token issued by a Cloudflare
account. That token cannot be in git, and unlike a provider API key it is not merely a credential
for an optional feature: without it there is no ingress at all, so the deployment described by this
repository does not exist without an account this repository cannot describe. `docker compose up`
would produce a system that is not the system.

**It puts an unauditable intermediary inside the trust boundary.** This project's entire thesis is
that a visitor can see what produced a number: the chunks, the scores, the spans, the cost, the
commit. With edge termination the TLS session ends at Cloudflare and a *second*, separate connection
carries the trace to the visitor. Every byte of the evidence passes through a party that is not the
origin, not the visitor, and not inspectable by either. For an ordinary web application that is a
reasonable trade for DDoS absorption and a global cache. For a glass box it converts the central
claim from "you can verify this" into "you can verify this, and also trust a CDN we did not tell you
about". That is an architecture characteristic being traded away for operational convenience, which
is precisely the trade ADR-0001 exists to make visible rather than silent — so it is written down
here as a rejection, not left as a preference somebody can quietly reverse.

The corollary is worth stating: this deployment has **no DDoS protection** beyond the per-address
token bucket in `garage/limits.py`, and a determined flood takes the site down. That is accepted. It
is a demonstration on a free VM, the failure mode is "unreachable" rather than "wrong", and
unreachable is a state the site can be in honestly.

## Why not nginx plus certbot

Two processes instead of one, a renewal timer, a reload hook, and a failure path that first becomes
visible ninety days after anybody last thought about it. The webroot-versus-standalone choice has to
be made and then remembered. None of that buys anything over Caddy here: there is one site, one
upstream and no legacy configuration.

## Consequences

- The certificate lives in the `caddy-data` volume. **That volume is load-bearing and its absence
  fails silently.** Let's Encrypt allows five duplicate certificates per registered domain per week;
  without the volume every `down && up` discards the certificate and requests a new one, so the
  fifth restart of a week succeeds and the sixth serves a TLS error for days, with nothing in the
  logs naming a rate limit. `docs/deploy.md` says this in capitals.
- Because TLS ends here, `X-Forwarded-For` written by Caddy is trustworthy, which is what makes
  per-address rate limiting possible at all. It is trusted only when
  `GARAGE_TRUST_FORWARDED_FOR=true`, set in the production overlay and nowhere else, and the
  rightmost element is the one honoured — see `limits.client_address`.
- Two ports are open to the internet and to nothing else: 80 and 443. Both of the OCI firewalls have
  to agree about that, and the second one is invisible from the repository; `docs/deploy.md` has the
  procedure and the one-minute diagnostic.
- The domain is an environment variable, so this decision does not wait on issue #15.
