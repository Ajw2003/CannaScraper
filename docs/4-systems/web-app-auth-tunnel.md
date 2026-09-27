# Web app, auth & tunnel

## What it owns

The FastAPI web UI, the admin-password gate on scraping actions, and the public-URL tunnel:
`server.py` (876 lines, routes), `app.py` (279 lines, the packaged entry point/banner),
`auth.py` (197 lines), `tunnel.py` (216 lines).

## How it works

- `server.py` is a thin layer over existing pieces — `catalog.search`, `stores.nearest`,
  `db.latest_observations`, and the fetchers — and adds no scraping logic of its own
  (`server.py:10-12`, `README.md:330-331`). `python server.py` serves local + LAN only, no
  tunnel; `app.py` is the same routes plus the tunnel and console banner, and is what the
  packaged exe runs (`server.py:5-9`).
- A live re-check takes 1-2s/store, so it runs as a background job (via `jobs.py`) with a
  progress endpoint rather than blocking the request (`server.py:14-15`,
  `GET /api/job/{id}` — `README.md:323`).
- Routes (`README.md:317-323`): `GET /api/provinces`, `GET /api/search`, `GET /api/results`
  (instant, index/cache), `POST /api/refresh` (starts a job), `GET /api/job/{id}`.
- **Auth model** (`auth.py:1-13`): reads (search, viewing stock) are open to anyone with the
  link. Writes — a live re-check, a province rebuild — need the admin password, because they
  spend the shared rate-limit budget and send traffic to cannacabana.com from this machine's IP.
  `check_password()` at `auth.py:86`; unauthenticated write attempts get `HTTPException(401,
  "password required")` (`auth.py:197`).
- **Loopback is deliberately not trusted as "the owner.":** cloudflared connects to
  `127.0.0.1`, so every request arriving through the public tunnel looks local to the process —
  trusting loopback would hand the admin surface to the whole internet (`auth.py:7-10`).
- Password storage: `settings.json` holds only a hash; the password is printed once on first
  run. Session cookies (`COOKIE = "cc_admin"`, `auth.py:29`) are signed with that hash, so
  changing the password signs out every session (`README.md:271-279`). Changeable via
  `Set password.bat` or `--set-password` while the app is running, without restart
  (`README.md:275-283`).
- **Tunnel** (`tunnel.py:1-18`): two modes — `quick` (`cloudflared tunnel --url ...`, no
  account, random `*.trycloudflare.com` hostname, best-effort/no SLA) and `named` (stable
  hostname via a Cloudflare account + token in `settings.json`). Child process output is relayed
  into the console rather than swallowed (`tunnel.py:19-23`). `CANNACABANA_TUNNEL_VERBOSE=1`
  shows all cloudflared log lines, not just curated ones (`tunnel.py:20`).
- Because the tunnel is HTTPS, browser geolocation ("📍 Near me") works over it but not over the
  plain-HTTP LAN URL (`README.md:267-269, 325-328`).
- Only one index/live job runs at a time (`jobs.py`, `MAX_HISTORY = 50` finished jobs retained,
  `jobs.py:28`) — see the fetchers/rate-limiting doc for why.

## Invariants

- Loopback (`127.0.0.1`) must never be treated as an authenticated caller — it's exactly what
  the tunnel makes every remote request look like (`auth.py:7-10`).
- Any route that causes outbound scraping must go through the password check; read-only routes
  must not (`auth.py:1-6`).
- `server.py` must not gain scraping logic of its own — it stays a thin layer reusing `catalog`,
  `stores`, `db`, and the fetchers (`server.py:10-12`).

## Traps

- Assuming an unauthenticated request arriving on `127.0.0.1` is trustworthy — it may be
  arriving through the public tunnel, not literally from the local machine (`auth.py:7-10`).
- Browsers refuse geolocation on plain `http://<lan-ip>` — this is a browser policy, not a bug in
  this app; the UI is expected to fall back to a manual location box (`README.md:325-328`).
- Quick tunnels are best-effort with no SLA and rotate hostnames on every start — not suitable
  for a link meant to keep working; use a named tunnel + token for that (`tunnel.py:9-14`).
