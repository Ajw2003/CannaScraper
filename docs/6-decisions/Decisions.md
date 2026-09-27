# Decisions

A running, dated, append-mostly log of what was decided, when, why, and what it replaced. Newest
entry at the top. Entries are never rewritten or deleted; the only edit an existing entry gets is
flipping its `Status` line to `Superseded`, pointing at the entry that replaced it.

---

## 2026-09-27 — Explore static GitHub Pages site fed by per-province GitHub Actions scrapers

**Context.** The current public-facing product is a Cloudflare tunnel out of one desktop machine
running the packaged `.exe` (roadmap M5) — fine for demo use, but the tunnel is best-effort with
no SLA in its default `quick` mode (`tunnel.py:9-14`), and it depends on that one machine staying
on. Today the user decided to explore moving the public site to something that doesn't depend on
a machine being left running: a static GitHub Pages site, fed by scheduled, per-province GitHub
Actions jobs that scrape and publish data to a public repo — modelled on
`Ajw2003/RockSkipping`'s `renew-cert.yml` pattern (a scheduled Action, a repo-scoped token,
publishing to a public repo).

**Decision.** Pursue this direction, starting with the smallest possible test of its central
unknown: whether cannacabana.com's catalog and stock API will even answer requests from GitHub
Actions runner IPs at all, rather than blocking them. That test —
`.github/workflows/runner-ip-test.yml` — is being built by a different, concurrent agent session
as of this writing; **its result is not yet known.** If it passes, the plan continues with
per-province scheduled scrape workflows, a public-repo publish step, and a static Pages site
reading that published data. Making the CannaScraper repo itself public is part of this plan,
because GitHub Actions minutes are free for public repos, whereas a private repo caps at 2,000
minutes/month and a daily all-province run is estimated at roughly 120 minutes/day (3,600+
minutes/month) — well over the private-repo allowance.

**Why.** Alternatives considered: keep the current tunnel-based model (rejected as a long-term
public product — no uptime guarantee, ties the product to one machine being on); a hosted server
(not explored yet — adds cost and ops surface the project has otherwise avoided, per
`README.md:6-7`'s "everything here is free" principle). The GitHub Actions approach keeps costs
at zero (same principle) if the repo is public, and removes the single-machine dependency. The
`RockSkipping` `renew-cert.yml` pattern was chosen as the reference because it's a proven,
working example of the same shape (scheduled Action, scoped token, publish to a separate
artifact) already in the user's own account.

**Status.** Standing, **conditional on the IP test in `runner-ip-test.yml` passing**. If
cannacabana.com blocks GitHub Actions runner IPs, this entire direction is blocked and needs a
different approach (see `docs/2-roadmap/Roadmap.md` M7's acceptance criteria). Recorded as
roadmap milestone M7.
