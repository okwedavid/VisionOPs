# VisionOps on Render (test deployment)

> **Promise:** one Render web service runs the API **and** serves the web
> console on the same HTTPS origin. No second service, no CORS, no database
> account. Deploy in about ten minutes.

This is the runbook for a **test** deployment to [Render](https://render.com)
using the committed [`render.yaml`](../render.yaml) Blueprint and
[`infra/docker/Dockerfile.render`](../infra/docker/Dockerfile.render).

Production deploys (own host, Postgres, nginx/TLS, backups) are covered in
[`DEPLOYMENT.md`](DEPLOYMENT.md); this page only covers Render.

---

## 1. Why a single service

The FastAPI app already serves the compiled SPA itself when
`<data_dir>/../frontend/dist` exists (`_spa_dir()` in `backend/visionops/main.py`).
`Dockerfile.render`:

1. builds the Vite SPA (`frontend/` → `dist`),
2. installs the backend with `uv sync --frozen --no-dev`,
3. copies the SPA to `/app/frontend/dist`,
4. bakes the pinned YOLO weights to `/app/models/yolo11n.onnx`.

The browser talks to `/api/v1` and the WebSocket on the **same host**, so there
is nothing cross-origin to configure and `VISIONOPS_CORS_ORIGINS` stays empty.

```
                    Render HTTPS (onrender.com)
                              │
                    ┌─────────▼──────────┐
        browser ───▶│  visionops (web)   │
                    │  uvicorn + SPA     │
                    │  /api/v1, /ws, /   │
                    │  SQLite @ /app/data│
                    └────────────────────┘
```

## 2. Prerequisites

| Item | Notes |
| --- | --- |
| Render account | Free tier is enough to try; see [§5](#5-data-persistence-read-this). |
| GitHub repo access | Render deploys from `https://github.com/okwedavid/VisionOPs`. |
| Local checkout | To generate the two secrets in [§3](#3-generate-the-secrets). |

No Docker needed locally — Render builds the image on its own builders.

## 3. Generate the secrets

Render will **not** generate these correctly for us, so create them locally and
paste them into the dashboard.

**API key** (format `vo_<12-hex>_<secret>`; the app rejects anything else and
would then create a key you can never see):

```bash
uv run --frozen python -c "from visionops.core.security import generate_api_key; print(generate_api_key().full_key)"
```

**Master key** (base64, 32 bytes — only needed to store RTSP credentials; leave
unset if you only use browser webcams):

```bash
uv run --frozen python -c "from visionops.core.security import generate_master_key; print(generate_master_key())"
```

Keep both values; they are the only copy.

## 4. Deploy

1. Push this repository (with `render.yaml` at the root) to GitHub.
2. In Render: **New → Blueprint**, pick the repo. Render reads `render.yaml` and
   creates one **Docker web service** named `visionops`.
3. Render prompts for the two `sync: false` variables:
   * `VISIONOPS_API_KEY` → paste the API key from [§3](#3-generate-the-secrets).
   * `VISIONOPS_MASTER_KEY` → paste the master key, or leave blank for
     browser-only testing.
4. Click **Apply**. The first build installs deps, builds the SPA and downloads
   the model (~11 MB) — expect a few minutes.

The service settings baked into the Blueprint:

| Setting | Value |
| --- | --- |
| Runtime | Docker |
| Dockerfile | `./infra/docker/Dockerfile.render` (context `.`) |
| Health check path | `/api/v1/healthz` |
| Start command | `visionops serve --host 0.0.0.0 --port $PORT` (Dockerfile `CMD`) |
| Region | `oregon` (change before first deploy if you prefer) |
| Plan | `free` (see below) |

Render injects `$PORT`; the Dockerfile `CMD` binds it. The app's `/api/v1/healthz`
is public, so the health check needs no credentials.

### Optional: run migrations before startup

Startup already migrates (`VISIONOPS_AUTO_MIGRATE=true`, run by `ensure_bootstrap`
in the lifespan). If you prefer a pre-deploy step, add to the service:

```yaml
    preDeployCommand: visionops migrate
```

## 5. Data persistence (read this)

**On the free plan the filesystem is ephemeral.** The SQLite database, snapshots
and media live under `/app/data` and are **erased on every deploy, restart, or
idle spin-down**. For a throwaway test this is fine; for anything you care about,
pick one:

* **Persistent disk (simplest, no code change).** Persistent disks require a
  paid instance. In `render.yaml` switch the plan and uncomment the disk:

  ```yaml
      plan: starter
      disk:
        name: visionops-data
        mountPath: /app/data
        sizeGB: 1
  ```

  Mounting at `/app/data` persists the DB/snapshots/media without shadowing the
  SPA at `/app/frontend/dist` or the model at `/app/models`. (A service with a
  disk can't be scaled horizontally — fine for one node.)

* **Render Postgres.** Managed and durable, but VisionOps needs the `psycopg`
  driver added to `pyproject.toml` (`psycopg[binary]`) and
  `VISIONOPS_DATABASE_URL=postgresql+psycopg://…`. Render's free Postgres expires
  after 30 days. Not wired up here on purpose to keep the image dependency set
  unchanged.

**Resource note.** The free instance has **512 MB RAM and 0.1 CPU**. ONNX
inference plus OpenCV will be tight; the Blueprint caps `VISIONOPS_MAX_SESSIONS=1`
to leave headroom. For real detection quality/speed, use `1c-2g` (1 GB+ is the
documented minimum, 2 GB comfortable). Free instances also **spin down when idle**
and cold-start on the next request.

## 6. Verify the deployment

Replace `<service>` with your `onrender.com` host:

```bash
curl -sS https://<service>.onrender.com/api/v1/healthz     # 200 {"status":"ok",...}
curl -sS https://<service>.onrender.com/api/v1/readyz      # 200 when model+DB ready
curl -sSI https://<service>.onrender.com/                  # 200, serves the SPA
```

Then open `https://<service>.onrender.com/` in a browser, paste the API key when
the console prompts, and run the Setup wizard (site → camera → source → zone →
**Start session**). No server-side camera is required: the browser webcam source
is captured client-side and streamed to the session WebSocket.

## 7. Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| Deploy fails while downloading the model | Build-time network blocked, or the pinned URL moved | Check the `visionops model` step in the build log; verify `VISIONOPS_MODEL_URL` / `VISIONOPS_MODEL_SHA256`. |
| `/api/v1/readyz` → `503` / `degraded` | Model or DB not ready | `readyz` lists which check failed. On free, confirm the model loaded (build log) and `/app/data` exists. |
| `/` returns the JSON banner, not the console | SPA not found at `/app/frontend/dist` | Ensure `VISIONOPS_DATA_DIR=/app/data` (parent must be `/app`) and that the web build stage succeeded. |
| Login always fails | `VISIONOPS_API_KEY` missing or malformed | It must match `vo_<12-hex>_<secret>`. Bootstrapping only honours it when no keys exist yet; if it was skipped, redeploy with the var set (and no existing keys). |
| Can't add an RTSP camera | `VISIONOPS_MASTER_KEY` unset/invalid | Set a 44-char base64 value (32 bytes) from [§3](#3-generate-the-secrets). |
| Session drops or OOM on free | 512 MB RAM / idle spin-down | Raise the plan (`1c-2g`), keep `VISIONOPS_MAX_SESSIONS=1`. |
| 401 on every API call from the console | API key rejected | Re-paste the key; confirm it wasn't revoked (`visionops key list` locally against the same DB). |

Health endpoints: `/api/v1/healthz` (liveness, always public, never touches the
DB) and `/api/v1/readyz` (readiness: `ready`/`degraded`/`unavailable`, `503` only
when a required check fails).

## 8. Updating

Push to `main` and Render redeploys automatically (`autoDeployTrigger: commit`).
Remember that on the free plan the data resets with each deploy. For a controlled
rollout, set `autoDeployTrigger: 'off'` and deploy from the dashboard.
