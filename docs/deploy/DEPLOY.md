# Deploying SearchHax (container, private)

SearchHax runs as two containers:

- **`api`**: the FastAPI exploration service, persisting to SQLite on a volume.
- **`web`**: the built React app served by nginx, which proxies `/api` to `api`.

This guide covers a **private** deployment on one machine. A public deployment needs
decisions that have not been made yet: authentication beyond basic auth, TLS, hosting, and
secrets management.

## 1. Configure

Provider keys are read from `.env` at runtime and are never baked into an image. Start from
`.env.example`. Leave the keys unset for a read-only deployment that can show stored
investigations but not run research.

| Variable | Where | Meaning |
|---|---|---|
| `TELEM_API_KEY`, `TAVILY_API_KEY`, `LLM_API_KEY`, `LLM_BASE_URL`, `LLM_MODEL` | `.env` → `api` | Provider access for research steps (billable). |
| `SEARCHHAX_DB_PATH` | `api` (set by compose) | SQLite file; `/data/exploration.sqlite` on the `searchhax-data` volume. |
| `SEARCHHAX_ALLOWED_ORIGINS` | `api` | Extra CORS origins, comma-separated. `*` is refused. Not needed behind the bundled nginx (same origin). |
| `SEARCHHAX_ALLOW_BILLABLE` | `api` (compose default `false`) | Server-side gate. When false, `/step`, `/branches/{id}/resume`, `/answer` and the live Story A baseline return 403 before any provider call. Unset outside compose means allowed (local development). |
| `VITE_ALLOW_BILLABLE_RESEARCH` | build arg for `web` | `false` by default: the UI cannot start research. `true` enables research behind an explicit confirmation. |
| `SEARCHHAX_BASIC_AUTH` | `web` | `off` (default) or a realm name to require HTTP basic auth. |
| `SEARCHHAX_HTPASSWD` | compose | Path to an htpasswd file mounted read-only when basic auth is on. |
| `SEARCHHAX_PORT` | compose | Host port, bound to `127.0.0.1` only (default `8080`). |

## 2. Build and run

```bash
docker compose build
```

```bash
docker compose up -d
```

```bash
curl -s http://127.0.0.1:8080/healthz
```

`/healthz` (and `/api/health`) reports `status`, `database`, `contract_version`, and whether
search and an LLM are configured, as booleans. It never returns key values.

Research is billable and needs **both** switches: the server gate and the UI flag.

```bash
VITE_ALLOW_BILLABLE_RESEARCH=true docker compose build web
```

```bash
SEARCHHAX_ALLOW_BILLABLE=true docker compose up -d
```

The backend still enforces each investigation's stored budget: at most 2 searches, 5 fetched
URLs and 10 LLM calls per investigation. It runs one step per investigation at a time, and
every write requires an idempotency key.

## 3. Access control

Ports are published on `127.0.0.1` only. Before exposing the app to anyone else, turn on
basic auth:

```bash
htpasswd -B -c ./deploy/htpasswd yourname
```

```bash
SEARCHHAX_BASIC_AUTH="SearchHax" SEARCHHAX_HTPASSWD=./deploy/htpasswd docker compose up -d web
```

Put TLS in front (a reverse proxy or the platform's load balancer). There are no per-user
accounts: everyone with access shares one research profile store.

## 4. Data and migrations

- All state is in the SQLite file on the `searchhax-data` volume. Missing tables are created
  at startup (`CREATE TABLE IF NOT EXISTS` in `exploration/backend/database.py`). There are no
  column migrations: fields added since 1.0 live in JSON columns, and contract changes are
  additive (`CONTRACT_VERSION` in `shared/contracts.py`; currently 1.2.0). Older rows
  therefore stay readable; for example, pre-1.1.0 branch rows resolve their root question by
  lineage. A future column change will need a real migration step. None exists yet.
- Back up with the API stopped, or use SQLite's online backup:

```bash
docker compose exec api python -c "import sqlite3; s=sqlite3.connect('/data/exploration.sqlite'); d=sqlite3.connect('/data/backup.sqlite'); s.backup(d)"
```

- To restore, stop `api`, replace `/data/exploration.sqlite` on the volume, and start it again.

## 5. Monitoring

- Container health checks: `api` probes `/api/health` every 30 s, and compose restarts
  unhealthy or crashed containers (`restart: unless-stopped`).
- Logs: `docker compose logs -f api web`. The API logs each request line; provider failures are
  recorded on the investigation (`blocker`, `metadata.last_error`) and shown in the UI.
- Cost: every investigation records `usage` (searches, fetched URLs, LLM calls, tokens). The
  API does not aggregate spend across investigations.

## 6. Private pilot vs public multi-user deployment

| Requirement | Private pilot (a few trusted users, one machine or VM) | Public / multi-user |
|---|---|---|
| Access | Basic auth (`SEARCHHAX_BASIC_AUTH` + htpasswd) behind TLS | Real accounts (OIDC/SSO), per-user sessions. **Not implemented.** |
| Data isolation | One shared store; everyone sees all investigations and one profile store | Per-user ownership of investigations, branches, profiles and knowledge assertions in every query. **Not implemented** (no user id in the schema). |
| Spend control | `SEARCHHAX_ALLOW_BILLABLE`, per-investigation budget caps, one step at a time per investigation | Per-user quotas and a global spend ceiling. **Not implemented** (usage is recorded per investigation only). |
| Database | SQLite on a backed-up volume | A server database (e.g. Postgres) with real migrations; SQLite allows one writer. **Not implemented.** |
| Secrets | `.env` on the host, never in images | A secrets manager and key rotation |
| Abuse | Trusted users only | Rate limiting, request size limits beyond nginx's 1 MB, and monitoring of provider errors and spend |
| Hosting / TLS | Your reverse proxy or a VM | Managed hosting, TLS, uptime monitoring and alerting |
| Status | **Ready** to run as described here once you choose a host | **Blocked** on the items marked not implemented |

## 7. Verified so far

See `docs/audit/M0-M1-audit.md` (section 11) for the dated local verification of this
setup. Nothing has been deployed to a public or shared host.
