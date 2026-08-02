# Deployment

The public deployment: one Oracle Ampere VM, three containers, a certificate that renews itself, and
a free tier protected by three limiters and a cache. This document is both the runbook and the
argument — the numbers below are choices, and a choice with no reasoning beside it is a number the
next person will change for no reason.

Read `docs/adr/0009-tls-terminates-on-the-origin.md` first if you are wondering why there is no
Cloudflare Tunnel.

---

## What is deployed

```
                    :80 :443
                       │
                  ┌────▼─────┐
                  │  caddy   │  ACME, HSTS, CSP, error page. TLS ends here, on this host.
                  └────┬─────┘
                       │  serve:8000  (compose network only)
                  ┌────▼─────┐
                  │  serve   │  FastAPI + retrieval + optional Gemini. Read-only.
                  └────┬─────┘
                       │  postgres:5432  (compose network only)
                  ┌────▼─────┐
                  │ postgres │  pgvector. The derived artifact (ADR-0002).
                  └──────────┘
```

Nothing but 80 and 443 is reachable from outside the host. The development `compose.yaml` publishes
5432 and 8000 so a developer can reach them; `deploy/compose.prod.yaml` resets both to nothing, and
that `!reset []` is the single most important line in the file while looking like a formatting
detail — a `ports:` list in an overlay is *appended* to the base, so writing nothing there would
leave a Postgres and an unauthenticated API on the public internet.

### Resource budget

Measured, not estimated: `serve` sits at roughly **790 MB** steady and peaks near **870 MB** during
an embedding batch; Postgres runs about **200 MB**; Caddy about **20 MB** idle. Total under
**1.5 GB** of the VM's 24 GB. This is written down so nobody has to re-litigate whether the stack
fits. It does, with an order of magnitude to spare.

---

## First deploy

### 1. Both OCI firewalls

There are two, they are independent, and the second one is invisible from this repository. **This is
the step that consumes the most time on a first deploy and it is not in any file here.**

1. **VCN Security List** (or Network Security Group): an ingress rule allowing TCP 80 and 443 from
   `0.0.0.0/0`.
2. **The host's own `iptables`.** Oracle's Ubuntu images ship a default `INPUT` chain that drops
   everything except 22, regardless of what the Security List says. Add the two ports and persist
   them, or the rules vanish on reboot:

   ```sh
   sudo iptables -I INPUT 6 -p tcp --dport 80  -m state --state NEW -j ACCEPT
   sudo iptables -I INPUT 6 -p tcp --dport 443 -m state --state NEW -j ACCEPT
   sudo netfilter-persistent save
   ```

**The one-minute diagnostic when the site is unreachable:** run `sudo tcpdump -ni any port 443` and
try to connect from outside. If you see the packet arrive, the Security List is fine and the host
firewall is dropping it. If you see nothing, the packet never reached the VM and the Security List
is the problem. Every other ordering of these two checks costs an afternoon.

### 2. DNS

An `A` record for `GARAGE_DOMAIN` pointing at the VM's public IP. ACME's HTTP-01 challenge needs the
name to resolve **before** Caddy starts, or the first certificate request fails.

### 3. The checkout and the secrets

```sh
sudo git clone https://github.com/G59-Toneli/garage /opt/garage
cd /opt/garage
sudo install -m 600 /dev/null .env
sudo editor .env
```

Two secrets, both in that file, neither ever in git:

```
GARAGE_GEMINI_API_KEY=…
ACME_EMAIL=you@example.org
GARAGE_DOMAIN=garage.example.org
```

No Docker secrets and no vault. ADR-0001 refuses "hardened production" infrastructure by name, and a
root-owned file at mode 600 is the proportionate answer for one provider key on a single-user VM. A
vault here would be ceremony that makes the deployment less reproducible, not more secure.

### 4. Pin the image

**The value committed in `deploy/compose.prod.yaml` today is a placeholder of sixty-four zeros**, and
it is deliberately not a blank: a file with no `image:` at all would silently fall back to building
from the checkout on the VM, which is the one thing this pinning exists to prevent. The placeholder
fails instead — but it fails *opaquely*, with a registry error about a manifest that does not exist,
so if you have skipped this step that error is the reason:

```
Error response from daemon: manifest for ghcr.io/g59-toneli/garage@sha256:0000… not found
```

CI pushes a multi-arch image to GHCR on every merge to `main` and prints the real digest as a
workflow notice. Replace the placeholder with it:

```yaml
image: ghcr.io/g59-toneli/garage@sha256:<digest>
```

**The digest, never a tag.** A tag is a mutable pointer, so `:main` on the VM is not a question the
repository can answer. With a digest, "what is running in production" is answered by reading a file
in git, updating production is editing that line and committing it, and rollback is `git revert`.
The git history *is* the deploy history. This is ADR-0001 and ADR-0002 applied to the deployment.

Nothing updates that line automatically. A workflow that redeployed on every merge would be
continuous deployment nobody chose.

### 5. Build the artifact, then serve

```sh
docker compose -f compose.yaml -f deploy/compose.prod.yaml pull
docker compose -f compose.yaml -f deploy/compose.prod.yaml run --rm serve python -m garage ingest
docker compose -f compose.yaml -f deploy/compose.prod.yaml up -d
```

`ingest` is a full rebuild and is always safe to re-run (ADR-0002). It also creates both Postgres
extensions, so there is no `initdb` step and no hand-copied SQL.

### 6. systemd

```sh
sudo cp deploy/garage.service /etc/systemd/system/
sudo systemctl enable --now garage
```

`restart: unless-stopped` already handles a Docker daemon restart. The unit handles a VM reboot, and
gives `systemctl status garage` as the single answer to "is this supposed to be running".

---

## THE ONE THING THAT BREAKS SILENTLY

> **`caddy-data` MUST BE A NAMED VOLUME. LET'S ENCRYPT ALLOWS FIVE DUPLICATE CERTIFICATES PER
> REGISTERED DOMAIN PER WEEK.**
>
> Without the volume, every `docker compose down && up` throws the certificate away and requests a
> new one. The fifth restart in a week still works. The sixth serves a TLS error, to everybody, for
> days, and **nothing in the logs says "rate limit"** until you go looking at Caddy's ACME output.
> The volume is already declared in `deploy/compose.prod.yaml`. Do not remove it, and do not
> `docker compose down -v`, which deletes it along with the database.

---

## Operations

### Reboot

`restart: unless-stopped` plus the systemd unit. `postgres-data` and `caddy-data` survive; the
answer cache and the rate-limit counters do not, and that is accepted — see "What resets" below.

On boot, `serve` compares the database's `corpus_hash` against this checkout's manifest and every
committed showcase record against the same hash (ADR-0002). A divergence is a **boot failure**, so
the healthcheck fails, Caddy has no healthy upstream, and visitors get
`deploy/unavailable.html` explaining that the artifact and the corpus diverge. That is the correct
outcome: serving numbers about material this artifact does not contain would look exactly as
authoritative as serving correct ones.

### Reingest

```sh
docker compose -f compose.yaml -f deploy/compose.prod.yaml run --rm serve python -m garage ingest
docker compose -f compose.yaml -f deploy/compose.prod.yaml restart serve
```

When the `corpus_hash` changes, every committed showcase record becomes stale **by construction** and
the next boot refuses. That is not a bug to work around: regenerating a showcase costs real money at
a paid provider, so it is a human action taken deliberately (`python -m garage showcase build`), never
an automatic consequence of a reingest.

### Embedder weights

Already solved by the image. The Dockerfile runs `python -m garage embedder fetch`, which verifies a
sha256 pinned in `garage/embedding.py` (ADR-0008). **The VM never downloads a weight file** — it
pulls an image that already contains them.

### Logs

`json-file`, `max-size: 10m`, `max-file: 3`, on all three services. Without rotation the disk fills
in a few months and the resulting outage looks mysterious rather than full.

```sh
docker compose -f compose.yaml -f deploy/compose.prod.yaml logs -f serve
```

### Is the budget gone?

```sh
curl -s https://$GARAGE_DOMAIN/provenance | jq .budget
```

`GET /provenance` publishes `generations_used` and `generations_remaining` for the current UTC day,
along with the `corpus_id` the fixture banner keys off and the commit this container was built from.
No shelling in required.

---

## The free tier, and what protects it

Three limiters, in memory, in one process, with no Redis. `garage/limits.py` argues the design at
length; this is the operator's view.

| limiter | key | default | on refusal |
|---|---|---|---|
| requests per minute | client address | 10, token bucket | **429** with `Retry-After` |
| generations per day | client address | 60 | degrade |
| generations per day | *global* | 200 | degrade |

**Zero means two different things.** `GARAGE_REQUESTS_PER_MINUTE=0` *disables* the anti-abuse
bucket; `GARAGE_GENERATION_BUDGET_PER_DAY=0` or `GARAGE_GENERATIONS_PER_DAY_PER_CLIENT=0` *refuse
every generation*. Both readings are wanted — a bucket of zero requests a minute is a policy nobody
wants, and a budget of zero generations is precisely how you run this site with retrieval and the
precomputed showcase and no provider spend at all.

**Retrieval is not limited at all.** It is local, free and about eleven milliseconds, and it is the
thing the project wants a visitor to do. Rate-limiting the free half to protect the paid half would
punish the visitor for using the product.

**Only the minute bucket returns 429.** Every other refusal serves 200 with the chunks, the scores
and the trace intact and an answer marked `degraded`. An error there would throw away the free half
of the product in order to report the unavailability of the paid half.

### What resets on restart

The counters and the cache are in memory, so a deploy or a crash hands the day's budget back. This is
accepted rather than overlooked. The real backstop is the provider's own 429, which the endpoint
already converts into the same degradation. Persisting the counters in Postgres would put a write
path into a service that is read-only today, which is a change of character (ADR-0002) and not worth
it for a number that is already backstopped.

Tracked addresses are capped at 4096 and evicted least-recently-seen. An evicted address gets its
per-client daily count back; that is tolerable because the *global* budget is keyed on nothing and
cannot be evicted, so the ceiling the provider actually bills against still holds. The per-client
limit is a fairness measure, not a cost control.

### The cache

Keyed on the question **plus** `strategy`, `k`, `tiers`, `contract`, `corpus_hash`, `embedder`,
`model` and `git_sha`. The last four make it invalidate itself on any redeploy or reingest; the
middle four are why the demo still demonstrates anything. `garage/cache.py` has the full argument,
and `tests/test_cache.py` has one test per axis.

---

## Degradation, stated honestly

When the day's generation budget is gone:

- **A curated showcase question** is answered from the committed record, identically, because the
  showcase lookup happens *before* the budget is consulted. It was paid for once and costs nothing
  now.
- **Any other question** gets retrieval, the scores, the overlap band and the trace, with the answer
  marked degraded.

That second line is the honest limit of "quota exhaustion degrades to the pre-computed path". There
is no precomputed answer for a question nobody curated, so the real cascade for free-form input is
**live → cache → retrieval-only degraded**. Claiming more would be a promise the deployment cannot
keep, and `tests/test_cascade.py` asserts the limitation as well as the feature.

There is a second limit, smaller and worth stating in a section with this title. **A curated question
is exempt from the two generation budgets but not from the anti-abuse bucket.** Ask more than ten
questions in a minute from one address and the eleventh gets a 429 whether or not it is curated —
because that limiter is not protecting the provider's quota, it is protecting an endpoint that
touches Postgres on every call. The wait is a couple of seconds and the message says so, but "the
curated questions are always available" is true of the quota and not of the flood.

**Two identical questions arriving at the same instant both generate.** There is no single-flight
lock around the cache, so a concurrent duplicate spends two generations instead of one and the second
result overwrites the first in the cache. This is accepted rather than overlooked: the `Lock` in
`limits.Limiter` is what keeps concurrency from *exceeding the ceiling*, which is the property that
costs money, and single-flight would add a second synchronisation primitive and a request waiting on
another request's provider call to save an occasional duplicate on a site with this much traffic.

---

## Verifying the overlay without the VM

The production overlay is otherwise unverifiable anywhere except in production, and an unverifiable
deployment file is a deployment file that is wrong.

```sh
docker compose -f compose.yaml -f deploy/compose.prod.yaml -f deploy/compose.local-tls.yaml up --build
```

`deploy/compose.local-tls.yaml` swaps two things and only two: `Caddyfile.local` (automatic HTTPS
off, plain HTTP on 8080, because ACME needs a public name) and a locally built image instead of the
pinned digest. Everything else is exercised for real — the reverse proxy, the header set, the error
page, and the fact that 5432 and 8000 are no longer published.

`auto_https off` rather than `tls internal`, deliberately: an internal CA issues a certificate no
client trusts, so every `curl` would need `-k` and the exercise would be testing the flag instead of
the configuration.

**What this does not prove**, and is only observable against the real domain: that ACME succeeds,
that renewal works, and that HSTS is honoured.

---

## Failure modes, in the order they actually happen

| symptom | most likely cause | check |
|---|---|---|
| Connection times out | one of the two OCI firewalls | `tcpdump -ni any port 443` — see the diagnostic above |
| TLS error after several restarts | Let's Encrypt duplicate-certificate limit | `docker compose logs caddy \| grep -i acme`; wait out the week |
| 502 with the explanation page | `serve` refused to boot — corpus/artifact divergence | `docker compose logs serve`; then `ingest` |
| Every answer says "orçamento diário" | budget spent for the UTC day | `curl /provenance \| jq .budget` |
| A curated question is answered live | the record's `corpus_hash` no longer matches | rebuild the showcase, or accept it |
| Answers look like the previous build | `GARAGE_GIT_SHA` not baked in | `curl /provenance \| jq .git_sha` — `unknown` means the cache key lost its build component |
| Every answer has an empty `answer` field | no `GARAGE_GEMINI_API_KEY` — a supported configuration | `curl /provenance \| jq .generation_configured`; the origin band says so too |
| `manifest … not found` on pull | the digest placeholder was never replaced | step 4 above |
