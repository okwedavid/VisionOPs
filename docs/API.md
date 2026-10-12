# VisionOps — API Reference

> **Status:** this document specifies the **implemented V0.1** contract and the **designed** target contract. Sections marked `NEXT` / `FUTURE` are API design commitments, not shipped endpoints. The machine-readable source of truth is the generated OpenAPI document at `/api/v1/openapi.json`.
> **Base URL:** `/api/v1` · **Content type:** `application/json` unless stated · **Timestamps:** ISO-8601 UTC with `Z`.

---

## 1. Conventions

### 1.1 Authentication

```http
Authorization: Bearer vo_9f3ac1b2d4e5_secretpart…
# or
X-API-Key: vo_9f3ac1b2d4e5_secretpart…
```

| Rule | Behaviour |
| --- | --- |
| Required for | Every `/api/v1/**` endpoint except `/healthz`, `/readyz`, `/docs`, `/openapi.json` |
| Not required for | Health/readiness (for probes), OpenAPI (contract discovery) |
| Failure | `401` with an identical body for unknown, revoked and malformed keys |
| Rotation | Create a second key, deploy it, revoke the first — overlapping validity |
| Scoping | A key belongs to exactly one organisation and can never read another |

### 1.2 Errors (RFC 9457 problem details)

```http
HTTP/1.1 422 Unprocessable Content
Content-Type: application/problem+json
X-Request-Id: 0192c4a1-7f3b-7c9d-9a11-6f0b2e5d8a31

{
  "type": "https://visionops.dev/problems/validation-error",
  "title": "Validation error",
  "status": 422,
  "detail": "The request body failed validation.",
  "instance": "/api/v1/zones",
  "code": "validation_error",
  "request_id": "0192c4a1-7f3b-7c9d-9a11-6f0b2e5d8a31",
  "errors": [
    { "field": "points[2][1]", "message": "must be between 0 and 1 (normalised coordinate)" }
  ]
}
```

| Status | `code` | When |
| --- | --- | --- |
| 400 | `bad_request` | Malformed request that Pydantic cannot even shape |
| 401 | `unauthorized` | Missing/invalid/revoked key (uniform) |
| 403 | `forbidden` | Authenticated but lacking a permission (`NEXT` with multiple users) |
| 404 | `not_found` | **Also** used for cross-organisation references (no existence oracle) |
| 409 | `conflict` | Duplicate name, duplicate primary source, session already running |
| 422 | `validation_error` | Field-level validation failure |
| 429 | `rate_limited` | Token bucket exhausted; `Retry-After` set |
| 500 | `internal_error` | Unexpected; carries `request_id`, nothing else |
| 503 | `capacity_exceeded` | `max_sessions` reached |
| 502 | `source_error` | A remote source (RTSP) failed on a synchronous operation |

Error handling rules: never a stack trace, SQL, file path or credential in an error body; every error is logged with the same `request_id`; 5xx always increments an error metric with the exception class as a low-cardinality label.

### 1.3 Pagination

List endpoints use **cursor pagination** on `(occurred_at, id)`-style stable ordering:

```http
GET /api/v1/events?limit=50&after=2026-01-01T10:15:42.318Z:0192c4a1-…

{ "items": [ … ], "next_cursor": "2026-01-01T10:02:11.004Z:0192c4a0-…", "has_more": true }
```

`limit` defaults to 50, max 200. Cursors are opaque to clients but stable under concurrent writes — a new event cannot make the page shift or duplicate an item.

### 1.4 Idempotency and concurrency (`NEXT`)

Mutating endpoints accept an optional `Idempotency-Key` header, and writes to `zones`/`cameras` accept `If-Match: <etag>` so two operators cannot silently overwrite each other's zone. V0.1 returns `ETag` for GETs on mutable resources; `If-Match` enforcement lands with multi-user editing (Phase 6).

### 1.5 Limits

| Limit | Default | Configurable |
| --- | --- | --- |
| Request body | 1 MB | `VISIONOPS_MAX_BODY_BYTES` |
| Page size | 200 | — |
| Zone polygon vertices | 256 | — |
| WebSocket frame | 2 MB | `VISIONOPS_WS_MAX_FRAME_BYTES` |
| Concurrent sessions | 4 | `VISIONOPS_MAX_SESSIONS` |
| Rate limit | 10 rps burst 30 per key | `VISIONOPS_RATE_LIMIT_RPS` |

---

## 2. Health and system

### `GET /healthz` — liveness (no auth)

```json
{ "status": "ok", "version": "0.1.0", "uptime_seconds": 3612.4 }
```

`200` if the process is alive. Never touches the database (a liveness probe must not fail because Postgres is briefly slow).

### `GET /readyz` — readiness (no auth)

```json
{
  "status": "ready",
  "timestamp": "2026-01-01T09:31:04.120000+00:00",
  "checks": {
    "api":      { "state": "ok", "reason_code": null, "detail": "API responding", "required": true },
    "database": { "state": "ok", "reason_code": null, "detail": "1.2 ms", "required": true },
    "storage":  { "state": "ok", "reason_code": null, "detail": "0.4 GB free", "required": false },
    "model":    { "state": "ok", "reason_code": null, "detail": "yolo11n ready", "required": false },
    "sessions": { "state": "ok", "reason_code": null, "detail": "no active sessions", "required": false }
  }
}
```

`200` when the API can serve requests; `503` when a **required** dependency (`api`, `database`) has failed, with `"status": "unavailable"`. Every check carries `state` (`ok` | `degraded` | `failed`), an optional `reason_code`/`detail`, and a `required` flag.

A **missing model or a single failing camera makes readiness `degraded`, not `unavailable`** — configuration, history and export still work, and the UI shows AI honestly rather than pretending the platform is down. The aggregation is: a required check failed → `unavailable`; anything else degraded or failed → `degraded`; otherwise `ready`.

`timestamp` is the server time (UTC, ISO-8601) at which the checks ran; clients use it together with their own clock to show staleness.

### `GET /system/status` — the human-facing health aggregate `CURRENT`

```jsonc
{
  "status": "degraded",              // healthy | degraded | failed
  "version": "0.1.0",
  "environment": "local",
  "timestamp": "2026-01-01T09:31:04.120000+00:00",
  "uptime_seconds": 3612.4,
  "model": {
    "state": "ready",                 // ready | loading | failed | disabled
    "name": "yolo11n", "sha256": "…", "provider": "CPUExecutionProvider",
    "input": {"width": 640, "height": 640}, "classes": 80,
    "warmup_ms": 210, "last_inference_ms": 38.2
  },
  "checks": {                         // api | database | storage | model | sessions
    "database": { "state": "ok", "reason_code": null, "detail": "1.2 ms", "required": true },
    "model":    { "state": "ok", "reason_code": null, "detail": "yolo11n ready", "required": false }
  },
  "sessions": {
    "active": 1, "max": 4,
    "items": [{
      "camera_id": "…", "camera_name": "front-door", "session_id": "…",
      "state": "running", "started_at": "…", "uptime_seconds": 640.1,
      "processed_fps": 4.8, "target_fps": 5.0, "frame_age_ms": 61,
      "frames_processed": 812, "frames_dropped": 3,
      "inference_ms": {"p50": 37.9, "p95": 44.1},
      "active_tracks": 3, "in_zone": 1,
      "events_emitted": 12, "events_suppressed": 4,
      "source": {"state": "running", "kind": "browser", "fps": 29.8,
                 "reason_code": null, "detail": null, "retries": 0}
    }]
  },
  "storage": { "database": "ok", "snapshot_dir": "disabled",
               "disk_free_bytes": 41238465536 },
  "counters": { "events_total": 12, "events_suppressed_total": 4,
                "inference_errors_total": 0, "source_errors_total": 0,
                "rate_limited_total": 0 }
}
```

This is the single source of truth for the System Status page. It is designed to be *readable by a human operator in 3 seconds* and to be the first thing a support request includes.

The verdict uses the same rule as `/readyz`: a **required** subsystem (`database`) failing is `failed`; an **optional** subsystem (`storage`, `model`, `sessions`) failing or degrading is `degraded`; everything healthy is `healthy`. A single camera losing its feed therefore degrades the platform report, never marks it failed.

### `GET /system/metrics` — Prometheus text exposition `CURRENT`

```
# HELP visionops_inference_latency_seconds Inference latency
# TYPE visionops_inference_latency_seconds histogram
visionops_inference_latency_seconds_bucket{le="0.05"} 812
visionops_inference_latency_seconds_bucket{le="0.1"} 820
visionops_inference_latency_seconds_sum 31.4
visionops_inference_latency_seconds_count 820
# TYPE visionops_pipeline_fps gauge
visionops_pipeline_fps{session="0192c4a1-…"} 4.8
# TYPE visionops_events_total counter
visionops_events_total{type="ZONE_ENTERED"} 12
```

Prometheus-compatible, zero dependencies. See `docs/OBSERVABILITY.md` for the full metric list.

### `GET /models/current` `CURRENT`

Returns the loaded model's `ModelInfo` plus the resolved configuration (thresholds, input size, class filter) so the UI can show exactly what the AI is doing.

---

## 3. Organisations, sites, cameras, sources, zones, events

> All endpoints below are **scoped to the caller's organisation**. There is no cross-organisation access.

### `GET /organizations/self` `CURRENT`

```json
{ "id": "…", "name": "Acme Logistics", "slug": "acme-logistics",
  "timezone": "Africa/Lagos", "retention_days": 90,
  "snapshots_enabled": false, "settings": {}, "created_at": "…" }
```

### Sites

| Method | Path | Notes |
| --- | --- | --- |
| `GET` | `/sites` | List, cursor-paginated, `?name=` filter |
| `POST` | `/sites` | 201; `name` unique per org |
| `GET` | `/sites/{site_id}` | 200/404 |
| `PATCH` | `/sites/{site_id}` | Partial update |
| `DELETE` | `/sites/{site_id}` | 409 if cameras exist |

```jsonc
// POST /api/v1/sites
{ "name": "Kano Warehouse", "timezone": "Africa/Lagos",
  "address": "Plot 12, Kano", "description": "Main distribution centre" }
```

### Cameras

| Method | Path | Notes |
| --- | --- | --- |
| `GET` | `/cameras` | Filters: `site_id`, `status`, `q` (name search) |
| `POST` | `/cameras` | 201; requires `site_id`, `name` |
| `GET` | `/cameras/{camera_id}` | Includes sources, zone count, live status |
| `PATCH` | `/cameras/{camera_id}` | Partial; `settings` merged shallowly |
| `DELETE` | `/cameras/{camera_id}` | Soft delete; history preserved |
| `GET` | `/cameras/{camera_id}/sources` | List sources (never credentials) |
| `POST` | `/cameras/{camera_id}/sources` | 201; `credentials` written once, never returned |

```jsonc
// POST /api/v1/cameras
{ "name": "front-door", "site_id": "…", "description": "Main entrance",
  "target_fps": 5, "settings": { "confidence_threshold": 0.35, "input_width": 640 } }

// POST /api/v1/cameras/{id}/sources
{ "name": "spare-rtsp", "kind": "rtsp", "target": "rtsp://192.168.1.20:554/stream1",
  "is_primary": false, "options": { "transport": "tcp", "timeout_ms": 5000 },
  "credentials": { "username": "viewer", "password": "…" }   // write-only
}
```

Response for a source:

```json
{ "id": "…", "camera_id": "…", "kind": "rtsp", "name": "spare-rtsp",
  "target": "rtsp://192.168.1.20:554/stream1",
  "is_primary": true, "options": {"transport":"tcp"},
  "credentials": { "configured": true },   // never the secret
  "created_at": "…" }
```

### Zones

| Method | Path | Notes |
| --- | --- | --- |
| `GET` | `/zones?camera_id=…` | List (enabled + disabled), newest first |
| `POST` | `/zones` | 201; `camera_id` required |
| `GET` | `/zones/{zone_id}` | |
| `PATCH` | `/zones/{zone_id}` | Partial; geometry re-validated |
| `DELETE` | `/zones/{zone_id}` | 204; soft delete; events keep `zone_name` |
| `GET` | `/zones/{zone_id}/events` | Convenience: filtered event list |

```jsonc
// POST /api/v1/zones
{
  "camera_id": "0192c4b0-…",
  "name": "restricted",
  "description": "Keep clear: electrical room",
  "kind": "polygon",
  "points": [[0.31,0.28],[0.72,0.28],[0.72,0.91],[0.31,0.91]],
  "color": "#f97316",
  "severity": "warning",
  "class_filter": ["person"],
  "cooldown_seconds": 10,
  "min_dwell_seconds": 0.5,
  "enabled": true
}
```

Validation rules (422 with field errors): ≥ 3 points; each coordinate in `[0,1]`; `kind ∈ {polygon,line}`; `color` matches `^#[0-9a-fA-F]{6}$`; `severity ∈ {info,warning,critical}`; `0 ≤ cooldown_seconds ≤ 86400`; `0 ≤ min_dwell_seconds ≤ 3600`; `class_filter` entries are non-empty strings. **Coordinates are normalised** — a client may send pixels only if it also sends `coordinate_space: "pixels"` together with the frame size, which the server converts (documented escape hatch for third-party clients).

### Events

| Method | Path | Notes |
| --- | --- | --- |
| `GET` | `/events` | Filters: `type`, `severity`, `camera_id`, `zone_id`, `from`, `to`, `q` (free text over names/type), `limit`, `after` |
| `GET` | `/events/{event_id}` | Full record with metadata |
| `GET` | `/events/summary` | Aggregates (below) |
| `GET` | `/events/export` | `format=csv\|json`, `from`/`to` required, streams a file |
| `POST` | `/events/{id}/acknowledge` | `NEXT` (single-operator V0.1: optional, unauthenticated by role) |
| `DELETE` | `/events/{id}` | **Deliberately absent** — evidence is not casually erased; see `docs/EVENT_ENGINE.md` §6 |

```jsonc
// GET /api/v1/events?camera_id=…&from=2026-01-01T00:00:00Z&type=ZONE_ENTERED&limit=2
{
  "items": [{
    "id": "0192c4a1-7f3b-7c9d-9a11-6f0b2e5d8a31",
    "schema_version": 1, "type": "ZONE_ENTERED", "severity": "warning",
    "occurred_at": "2026-01-01T10:15:42.318Z",
    "received_at": "2026-01-01T10:15:42.402Z",
    "camera_id": "…", "camera_name": "front-door",
    "zone_id": "…", "zone_name": "restricted",
    "track_id": 12, "session_id": "…", "confidence": 0.91,
    "metadata": { "box": [0.31,0.22,0.44,0.86], "foot_point": [0.375,0.86],
                  "class": "person", "dwell_seconds": null, "model": "yolo11n" },
    "snapshot_path": null
  }],
  "next_cursor": "2026-01-01T10:02:11.004Z:0192c4a0-…",
  "has_more": true
}
```

### `GET /events/summary` `CURRENT`

```json
{
  "window": { "from": "2026-01-01T00:00:00Z", "to": "2026-01-02T00:00:00Z" },
  "totals": { "events": 128, "by_severity": {"info": 40, "warning": 80, "critical": 8, "error": 0} },
  "by_type":   [ {"type": "ZONE_ENTERED", "count": 96}, {"type": "ZONE_EXITED", "count": 32} ],
  "by_camera": [ {"camera_id": "…", "camera_name": "front-door", "count": 128,
                  "entries": 96, "exits": 32, "avg_dwell_seconds": 14.2} ],
  "by_zone":   [ {"zone_id": "…", "zone_name": "restricted", "count": 128} ],
  "hourly":    [ {"bucket": "2026-01-01T10:00:00Z", "count": 12} ]
}
```

Computed with a single grouped query per dimension, bounded to a 31-day window (`422` beyond that with a hint to use the export endpoint). `NEXT`: pre-materialised hourly aggregates for wide windows.

---

## 4. Sessions and live data

### `POST /cameras/{camera_id}/session` — start `CURRENT`

```jsonc
// request
{ "source_id": "0192c4b2-…",       // optional: defaults to the primary source
  "target_fps": 5,                   // optional: overrides camera default
  "zones": ["0192c4c0-…"] }          // optional: limit to specific zones
// 201
{ "session_id": "0192c4c5-…", "camera_id": "…", "state": "starting",
  "started_at": "…", "target_fps": 5.0 }
```

Idempotent: if a session is already running for the camera, the existing session is returned with `200` instead of `409`. `503 capacity_exceeded` when `max_sessions` is reached. For `kind: "browser"` sources, the response includes the WebSocket URL the page must connect to:

```json
{ "session_id": "…", "state": "starting", "ingest_ws_url": "/api/v1/stream/ingest?session_id=…" }
```

### `GET /cameras/{camera_id}/session` — current state `CURRENT`

Same shape as the item in `/system/status.sessions.items`.

### `DELETE /cameras/{camera_id}/session` — stop `CURRENT` → `204`

Stops gracefully: the pipeline finishes the current frame, flushes the event queue, records `SOURCE_STOPPED`, and closes the WebSocket.

### `GET /cameras/{camera_id}/preview.mjpg` — MJPEG `CURRENT`

`multipart/x-mixed-replace; boundary=frame`. Frames are the annotated stills produced by the pipeline, capped at `preview_fps` (default 5) and `preview_width` (default 640). Only for server-side sources (`device`/`file`/`rtsp`) — the browser source renders its own local preview and never round-trips video. Supports `curl`/`ffplay` for debugging.

### `WS /api/v1/stream/sessions/{session_id}` — live state `CURRENT`

Server → client JSON frames (see `docs/ARCHITECTURE.md` §3.2 for the full payload): `session.state` (throttled), `session.event` (on transitions), `session.error` (degradation), `pong`. Client → server: `{"type":"ping"}`, `{"type":"set_publish_fps","value":10}`.

Behaviour: heartbeat every 20 s; clients that miss two rounds are dropped; a slow client's intermediate *states* are dropped (they are not durable) — the client shows a "stale" indicator and re-syncs events over REST. Events are never dropped: the socket is an accelerator, the database is the truth.

### `WS /api/v1/stream/ingest?session_id=…` — browser frame ingest `CURRENT`

Binary **JPEG** frames, one per message, plus an initial text JSON hello message:

```json
{ "type": "ingest.hello", "session_id": "…", "target_fps": 5,
  "width": 1280, "height": 720, "max_frame_bytes": 2097152 }
```

The client then sends JPEG frames as binary messages, pacing itself to `target_fps`. The server decodes, timestamps and enqueues. Frames are never persisted. A malformed frame is dropped and counted (`frames_invalid_total`), not fatal.

---

## 5. API keys (self-service for the current operator) `CURRENT`

| Method | Path | Notes |
| --- | --- | --- |
| `GET` | `/apikeys` | Metadata only: `id`, `name`, `key_prefix`, `last_used_at`, `revoked` |
| `POST` | `/apikeys` | 201 — **the only time the secret is returned** |
| `DELETE` | `/apikeys/{id}` | Revoke (never hard delete, for audit) |

```jsonc
// POST /api/v1/apikeys  → 201
{ "id": "…", "name": "ops-laptop",
  "key": "vo_9f3ac1b2d4e5_Zm9…",      // shown exactly once
  "key_prefix": "vo_9f3ac1b2d4e5", "warning": "Store this now. It cannot be retrieved later." }
```

Requires an existing key (bootstrap only happens when zero keys exist).

---

## 6. `NEXT` — designed, not shipped

| Group | Endpoints |
| --- | --- |
| Users & RBAC | `POST/GET /users`, `POST /members/invitations`, `GET /members`, `PATCH /members/{id}` (role), `DELETE /members/{id}`, `POST /auth/login`, `POST /auth/logout`, `GET /auth/me` |
| Zones (extended) | `POST /zones/{id}/duplicate`, `POST /zones/import`, `GET /zones/templates`, line-zone fields (`points` length 2 + `direction`) |
| Rules | `GET/POST /rules`, `GET/PATCH/DELETE /rules/{id}`, `POST /rules/{id}/test`, `GET /rules/{id}/simulate` (replay a window and show what *would* have fired) |
| Alerts | `GET /alerts`, `POST /alerts/{id}/acknowledge`, `POST /alerts/{id}/resolve`, `GET/POST/PATCH /alert-policies` |
| Analytics | `GET /analytics/occupancy?zone_id&from&to`, `GET /analytics/dwell`, `GET /analytics/heatmap` (time × hour), `GET /analytics/baselines` |
| Reports | `GET/POST /reports`, `GET /reports/{id}`, `GET /reports/{id}/download`, `POST /reports/{id}/run` |
| Notifications | `GET /notifications`, `POST /notification-channels` (SMTP/Slack/webhook/SMS), `POST /notification-channels/{id}/test` |
| Retention admin | `GET /retention`, `PATCH /retention`, `POST /retention/purge` (org-level erasure), `GET /retention/schedule` |
| Webhooks | `GET/POST /webhooks`, `GET /webhooks/{id}/deliveries`, `POST /webhooks/{id}/replay` |
| Integrations | `GET/POST /integrations`, `GET /integrations/providers` |
| Usage/billing | `GET /usage`, `GET /subscription`, `POST /subscription/portal` |

## 7. `FUTURE` — designed, not shipped

Public data API (anonymous, read-only, aggregates only): `GET /public/v1/orgs/{slug}/occupancy`, `GET /public/v1/orgs/{slug}/stats` — **no camera, zone or event geometry is ever exposed**, and the response is rate-limited per IP with a small cache TTL.

---

## 8. Versioning and compatibility

- Major version in the path: `/api/v1`. Minor versions are additive: new endpoints, new optional response fields, new event types (with `schema_version`).
- Breaking changes (removing/renaming a field, changing a type or a meaning, tightening validation on an existing field) require `/api/v2`, and `/api/v1` stays available for **≥ 12 months** with `Deprecation` and `Sunset` headers.
- Consumers must **ignore unknown fields** — that is how additive change stays safe.
- Event consumers must tolerate unknown `type` values (skip + log). `GET /events/schema` describes the known types.
- Every change is recorded in `CHANGELOG.md`; the OpenAPI document is generated from the code, so documentation cannot drift from behaviour.

## 9. Discovery

| Path | Purpose |
| --- | --- |
| `/api/v1/openapi.json` | OpenAPI 3.1 document (generated) |
| `/api/v1/docs` | Interactive Swagger UI |
| `/api/v1/redoc` | ReDoc |
| `/api/v1/events/schema` | Event type catalogue with metadata schemas |
