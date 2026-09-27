# ContractGuard — Backend

A FastAPI service that compares OpenAPI specifications and surfaces breaking changes.

---

## Prerequisites

| Tool | Minimum version |
|------|----------------|
| Python | 3.11 |
| [uv](https://docs.astral.sh/uv/) | 0.4+ |
| Docker (+ Compose) | 24+ |

---

## 1. Install dependencies

Run the following command from the **`backend/`** directory (the folder that contains
`pyproject.toml`):

```bash
uv sync
```

`uv sync` reads `pyproject.toml` / `uv.lock`, creates a virtual environment at
`.venv/` if one does not already exist, and installs all pinned dependencies into
it. No separate `pip install` step is needed.

---

## 2. Environment variables

Create a file named **`.env`** directly inside **`backend/`** (i.e., at the same
level as `pyproject.toml`, _not_ inside `contractguard/`):

```
backend/
├── .env              ← place the file here
├── pyproject.toml
└── contractguard/
    └── diff_agent.py
```

### Required variables

| Variable | Purpose | Example value |
|----------|---------|---------------|
| `SECRET_KEY` | Signs JWT tokens — must be a long random string | `changethis_use_openssl_rand_hex_32` |
| `PROJECT_NAME` | Displayed in the auto-generated API docs title | `ContractGuard` |
| `DATABASE_URL` | SQLAlchemy-compatible connection string for PostgreSQL | `postgresql+psycopg://user:pass@localhost:5432/contractguard` |
| `FIRST_SUPERUSER` | E-mail address of the initial admin account | `admin@example.com` |
| `FIRST_SUPERUSER_PASSWORD` | Password for the initial admin account | `changethis` |

### Minimal `.env` example

```dotenv
SECRET_KEY=replace_with_a_real_secret_at_least_32_chars
PROJECT_NAME=ContractGuard
DATABASE_URL=postgresql+psycopg://contractguard:contractguard@localhost:5432/contractguard
FIRST_SUPERUSER=admin@example.com
FIRST_SUPERUSER_PASSWORD=changethis
```

> **Security:** Never commit `.env` to version control. A `.env.example` with
> placeholder values is safe to commit; the real `.env` file should be listed in
> `.gitignore`.

---

## 3. Start the server

```bash
# from backend/
uv run uvicorn contractguard.main:app --reload --host 0.0.0.0 --port 8000
```

The API will be available at `http://localhost:8000`.

---

## 4. API prefix

Every endpoint is served under the **`/api/v1`** prefix. Key URLs:

| Resource | URL |
|----------|-----|
| Interactive docs (Swagger UI) | `http://localhost:8000/docs` |
| ReDoc | `http://localhost:8000/redoc` |
| OpenAPI schema (JSON) | `http://localhost:8000/api/v1/openapi.json` |
| Health check | `http://localhost:8000/api/v1/health` |

When calling any endpoint directly (e.g., from a frontend or `curl`), always
include the full `/api/v1/...` prefix:

```bash
curl http://localhost:8000/api/v1/openapi.json
```

---

## 5. Troubleshooting

### Server does not start or behaves unexpectedly

Before investigating further, check for port conflicts and verify that required
containers are running:

```bash
# 1. List running containers — confirm the PostgreSQL container is Up
docker ps

# 2. Check whether ports 8000 (API) and 5432 (PostgreSQL) are already in use
#    on Windows (PowerShell)
netstat -ano | Select-String ":8000|:5432"

#    on macOS / Linux
lsof -i :8000 -i :5432
```

Common causes:

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| `address already in use :8000` | Another process is bound to port 8000 | Stop the other process or change the `--port` flag |
| `could not connect to server` (Postgres) | PostgreSQL container is not running or port 5432 is taken | Run `docker ps`; if the container is absent, run `docker compose up -d db` |
| `422 Unprocessable Entity` on login | `FIRST_SUPERUSER` / `FIRST_SUPERUSER_PASSWORD` not set or `.env` is in the wrong directory | Confirm `.env` lives inside `backend/`, not `backend/contractguard/` |
| `401 Unauthorized` on all requests | `SECRET_KEY` mismatch between server restart | Ensure `SECRET_KEY` is consistent across restarts; tokens signed with an old key are invalid |

### Re-run database migrations

```bash
uv run alembic upgrade head
```

### Reset and reseed the database

```bash
docker compose down -v          # removes volumes — destroys all data
docker compose up -d db
uv run alembic upgrade head
```
