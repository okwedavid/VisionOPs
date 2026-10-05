# VisionOps — Technical Decisions (ADR log)

> Format: **Context → Decision → Alternatives → Consequences (disadvantages, migration, revisit trigger)**.
> Status values: `Accepted`, `Accepted (provisional)`, `Superseded`.
> Every entry states *when it should be reconsidered*. Decisions without a revisit trigger are cargo-culting.

## Index

| ADR | Decision | Status |
| --- | --- | --- |
| [ADR-001](#adr-001-language-split-python-for-cv--typescript-for-the-ui) | Language split: Python (CV/backend) + TypeScript (UI) | Accepted |
| [ADR-002](#adr-002-single-process-modular-monolith-not-microservices) | Modular monolith, single process | Accepted |
| [ADR-003](#adr-003-fastapi--pydantic-v2-for-the-api) | FastAPI + Pydantic v2 | Accepted |
| [ADR-004](#adr-004-sqlalchemy-2-orm--alembic-migrations) | SQLAlchemy 2.0 + Alembic | Accepted |
| [ADR-005](#adr-005-sqlite-default-postgres-compatible) | SQLite default, Postgres compatible | Accepted |
| [ADR-006](#adr-006-onnx-runtime--yolo-family-onnx-model) | ONNX Runtime + YOLO-family ONNX | Accepted |
| [ADR-007](#adr-007-in-house-nms-and-preprocessing) | In-house letterbox + NMS | Accepted |
| [ADR-008](#adr-008-self-contained-kalman--iou-tracker-no-bytezones-dependency) | Self-contained Kalman+IoU tracker | Accepted |
| [ADR-009](#adr-009-browser-webcam-as-a-first-class-video-source) | Browser webcam as a first-class source | Accepted |
| [ADR-010](#adr-010-websocket-for-live-state--mjpeg-for-preview) | WebSocket state + MJPEG preview | Accepted |
| [ADR-011](#adr-011-react--vite--tailwind--tanstack-query) | React + Vite + Tailwind + TanStack Query | Accepted |
| [ADR-012](#adr-012-polygon-zones-with-foot-point-containment) | Polygon zones + foot-point containment | Accepted |
| [ADR-013](#adr-013-events-as-the-only-durable-product) | Events are the only durable product | Accepted |
| [ADR-014](#adr-014-api-key-authentication-in-v01) | API-key authentication in V0.1 | Accepted (provisional) |
| [ADR-015](#adr-015-protocol-based-pluggable-vision-interfaces) | Protocol-based pluggable vision interfaces | Accepted |
| [ADR-016](#adr-016-in-house-json-logging-and-metrics-registry) | In-house JSON logging + metrics registry | Accepted |
| [ADR-017](#adr-017-standard-library-cryptography-for-secret-encryption) | stdlib `cryptography` for secret encryption | Accepted |
| [ADR-018](#adr-018-no-face-recognition-anywhere--ever) | No facial recognition, ever | Accepted (permanent) |
| [ADR-019](#adr-019-normalised-coordinates-everywhere) | Normalised coordinates everywhere | Accepted (permanent) |
| [ADR-020](#adr-020-model-download-on-first-run-with-offline-mode) | Model auto-download with offline mode | Accepted |
| [ADR-021](#adr-021-commitments-to-dependencies) | Dependency policy: minimal, pinned, justified | Accepted |
| [ADR-022](#adr-022-testing-strategy-scripts--fixtures-over-real-hardware) | Scripted detectors + fixtures, not hardware | Accepted |
| [ADR-023](#adr-023-makefile-as-the-single-task-entrypoint) | Makefile as the single task entrypoint | Accepted |
| [ADR-024](#adr-024-time-ordered-uuids-for-events) | Time-ordered UUIDs for events | Accepted |
| [ADR-025](#adr-025-no-message-broker-in-v01) | No message broker in V0.1 | Accepted |

---

## ADR-001: Language split — Python for CV, TypeScript for the UI

**Context.** VisionOps needs (a) a real-time UI with a dense operator dashboard and (b) an efficient CPU-first object-detection and tracking pipeline. These are the two most capability-dense parts of the product, and they live in different ecosystems.

**Decision.** Backend, vision engine, event engine and CLI are **Python 3.11+**. Frontend is **TypeScript**. The contract between them is the HTTP/WebSocket API, defined by **generated OpenAPI** and a hand-maintained WS message schema.

**Alternatives considered.**
- *All TypeScript*: `onnxruntime-node` works, and `@tensorflow/tfjs-node` exists, but camera access, video decoding and the tracker ecosystem are weak, and I would be hand-writing everything that Python gives away. Rejected: the CV core would be our weakest component, and the CV core is the product.
- *All Python UI* (Streamlit/Dash/HTMX): fast to prototype, but a 6-year product cannot ship an operator-grade responsive dashboard on them. Rejected for the long horizon.
- *Go/Rust backend*: attractive for concurrency, but every ML and CV library would become a subprocess or a binding. Rejected for V0.1 efficiency.
- *One language + a separate CV microservice*: same two-language problem plus deployment overhead. Deferred — see ADR-002.

**Consequences.**
- *Disadvantages*: two toolchains, two lockfiles, two test runners; contract drift is possible; CI runs two jobs. Type duplication between Pydantic schemas and TS types.
- *Mitigation*: OpenAPI-generated client types, a shared WS message schema documented in one place (`docs/API.md`), one `make` entrypoint, and CI that fails if the OpenAPI spec changes without regenerating types.
- *Migration*: the vision engine is pure Python; if a future edge component needed Rust, only `vision/` would be rewritten and behind the same `Protocol`s.
- *Reconsider when*: never for the vision core. Revisit the *backend* language if async scale ever demands a compiled runtime — the API layer is deliberately thin so that a FastAPI→Go rewrite touches `api/` and `services/` only.

## ADR-002: Single-process modular monolith, not microservices

**Context.** V0.1 is one camera, one operator, one machine. The brief explicitly forbids unnecessary microservices.

**Decision.** One deployable process containing the API, the WebSocket hub and the pipeline workers. Modules are separated by **package boundaries and dependency direction** (`api → services → {db, ingest, realtime}`, `ingest → vision`), not by network calls.

**Alternatives considered.** API + worker + broker (Celery/Redis/RabbitMQ); serverless; k8s-native. All rejected: they add operational surface, latency, and a data-residency story for zero benefit at this scale.

**Consequences.**
- *Disadvantages*: a pipeline bug can in principle affect the API (mitigated: CV code cannot import web code, and session threads are isolated; a hard crash of the process takes everything down — hence systemd/Docker restart policies). Horizontal scaling requires extraction.
- *Migration path is explicit*: `SessionManager` owns thread lifecycle; `EventSink` owns fan-out; `VideoSource` owns frames. Phase 2 extracts `SessionManager` + pipeline into a worker process communicating over a database queue and a `EventSink` broker implementation. No caller changes except DI wiring.
- *Reconsider when*: > ~8 concurrent sessions per host, or when independent pipeline scaling becomes necessary (Phase 2/10).

## ADR-003: FastAPI + Pydantic v2 for the API

**Context.** Need typed, validated, documented HTTP + WebSocket endpoints with good async behaviour and low operational weight.

**Decision.** FastAPI (ASGI) with Pydantic v2 models as the request/response contract, Uvicorn as the server.

**Alternatives considered.** Django + DRF (batteries, but admin/ORM assumptions and sync views are a poor fit for a live-streaming API); Litestar (excellent, less ecosystem); raw Starlette (too little structure); Express/Nest (contradicts ADR-001).

**Consequences.**
- *Disadvantages*: Pydantic v2 migration friction for those used to v1; FastAPI's automatic docs are only as good as our type annotations, so we annotate rigorously (mypy `disallow_untyped_defs = true`); dependency set is moderate (fastapi, pydantic, starlette, uvicorn, anyio…).
- *Benefits worth the cost*: OpenAPI generated from code (never hand-written docs), automatic 422 validation errors, native WebSockets, small footprint, excellent typing story with Pydantic v2.
- *Migration*: routers are thin (validate → service → map), so a framework swap touches `api/` only.
- *Reconsider when*: never foreseeable; FastAPI is the safe default in this space.

## ADR-004: SQLAlchemy 2.0 ORM + Alembic migrations

**Context.** V0.1 needs 8 tables with clean migrations; Phase 6 needs RLS and multi-user queries.

**Decision.** SQLAlchemy 2.0 typed ORM (`Mapped[...]`/`mapped_column`) + Alembic for migrations, applied on startup in production mode.

**Alternatives considered.** Raw SQL + `sqlite3`/psycopg (transparent but no migrations, no type safety, error-prone at scale); Tortoise ORM; SQLModel (couples schema to API — rejected, it would make DB and API contracts inseparable); Drizzle (TS-only, contradicts ADR-001).

**Consequences.**
- *Disadvantages*: two dialects to respect (SQLite lacks `JSONB` — we use portable `JSON`); sync ORM means we do not run sessions on the event loop (correct, but must be respected in review); migrations are a real artefact to maintain.
- *Migration*: the ORM is confined to `models/` + `db/`; moving to async SQLAlchemy or to raw SQL for hot paths (event inserts at scale) is local and incremental.
- *Reconsider when*: if the event insert path becomes the bottleneck (Phase 3), we will batch raw inserts *within* SQLAlchemy cores rather than leave the ORM.

## ADR-005: SQLite default, PostgreSQL compatible

**Context.** "Runs locally with zero infrastructure" is a core product promise. Small deployments must work on a $90 SBC.

**Decision.** SQLite (WAL mode, `busy_timeout`) is the default and fully supported. Every query is written to run on both SQLite and Postgres; the URL is configuration.

**Alternatives considered.** Postgres-only (violates the zero-install promise); Postgres-only-in-Docker (violates "no Docker required"); DuckDB (wrong workload).

**Consequences.**
- *Disadvantages*: single-writer concurrency; no native `JSONB`/indexing niceties; migrations must be dialect-aware; no network access to the DB (matters for containers — documented).
- *Benefits*: zero-install local dev, trivially backed up (one file), ideal for edge boxes.
- *Migration*: dialect-specific features are quarantined in `db/`; partitioning and RLS are Postgres-only features behind `docs/DATABASE.md` guards and covered by dialect-aware migration code.
- *Reconsider when*: > ~1 M events or > 1 concurrent writer → Postgres (documented threshold; Phase 6).

## ADR-006: ONNX Runtime + YOLO-family ONNX model

**Context.** Need accurate, fast, licence-friendly person detection on CPU, deployable offline, on ARM, and later on GPU/NPU.

**Decision.** Run a YOLO-family model (default `yolo11n`) in ONNX form with **ONNX Runtime**, CPU execution provider by default, provider configurable.

**Alternatives considered.**
- *PyTorch + `ultralytics` at runtime*: simplest code, but pulls ~800 MB (torch + CUDA libs) and slows every install. Rejected for runtime; kept as an *optional* `ml` extra for export/evaluation only.
- *TFLite*: excellent for mobile/edge, weaker for desktop CPU.
- *OpenVINO*: faster on Intel CPUs, more deployment complexity.
- *TensorRT`: fastest on NVIDIA, requires NVIDIA. Optional provider later.
- *Managed cloud CV API*: privacy, cost, latency, offline-capability — all disqualifying.

**Consequences.**
- *Disadvantages*: ONNX opset/export compatibility must be managed; ORT version upgrades occasionally change numerics slightly; no built-in NMS in the graph means we own post-processing (mitigated by ADR-007); model file must be downloaded or provided (ADR-020).
- *Benefits*: ~15 MB runtime vs ~800 MB; fast CPU inference; ARM64 wheels; offline; provider abstraction for GPU/NPU later; permissive model licences.
- *Migration*: another runtime means a new `VisionModel` adapter (ADR-015) — nothing else changes.
- *Reconsider when*: we need accuracy beyond what a YOLO-class model provides at the edge → evaluate RT-DETR/other families behind the same interface, choosing by *measured* metrics per site, not hype.

## ADR-007: In-house letterbox + NMS

**Context.** YOLO post-processing (letterbox transform, tensor→box rescale, class filtering, NMS) is ~80 lines and every framework implements it slightly differently.

**Decision.** Implement it ourselves in NumPy, with the model I/O contract (NCHW float32, `[0,1]` normalisation, `xywh`/`xyxy` output shapes) declared in the adapter. Fully unit-tested against hand-computed cases.

**Alternatives considered.** `torchvision.ops.nms` (requires torch at runtime — defeats ADR-006); `ultralytics.utils.ops` (requires ultralytics); OpenCV `dnn.NMSBoxes` (works, but adds a second code path with different coordinate conventions and less control over the score-fusion options ByteTrack-style tracking wants).

**Consequences.**
- *Disadvantages*: we own correctness. Mitigated by deterministic unit tests (known boxes → known output), including a hand-computed IoU matrix case.
- *Benefits*: no torch dependency at runtime; full control for class-wise and soft-NMS variants later; identical numerics across providers.
- *Reconsider when**: if we adopt a model family with fundamentally different output (e.g. DETR with native NMS in-graph) — then it belongs in that model adapter, not in shared code.

## ADR-008: Self-contained Kalman+IoU tracker, no ByteTrack dependency

**Context.** Zone enter/exit events require stable track IDs. A naive IoU-only tracker produces ID switches at occlusion boundaries, which produce duplicate or phantom events.

**Decision.** Implement a self-contained tracker: constant-velocity Kalman filter per track (4-state position + velocity, in normalised coordinates), IoU-based association in two passes (high-confidence detections first, then low-confidence ones against *lost* tracks), linear re-association of recently lost tracks, and a bounded `max_age`.

**Alternatives considered.** `ultralytics.trackers.ByteTracker` (would pull ultralytics+torch into the runtime — ADR-006 violation); `norbottracker` (extra heavy dependency, larger memory); a pure IoU tracker without motion (rejected: poor under motion); optical flow (rejected: expensive, fragile).

**Consequences.**
- *Disadvantages*: we own tracker quality; our tracker is *not* state-of-the-art (it is roughly ByteTrack-lite: two-stage association but no Kalman-driven track synthesis of high-confidence boxes). It is nonetheless a large improvement over IoU-only and is fully deterministic and testable.
- *Honest limitations*: dense crowds and heavy occlusion cause ID switches; we mitigate with cooldowns and dedup at the event layer, and document the limits.
- *Migration*: a better tracker (ByteTrack/DeepSORT/BoT-SORT) is a `Tracker` adapter; the `Track` dataclass is the contract.
- *Reconsider when*: field data shows unacceptable ID-switch rates in real deployments, or per-vertex crowding appears — the interface makes this a drop-in change with an evaluation harness to prove it is better.

## ADR-009: Browser webcam as a first-class video source

**Context.** The headline V0.1 use case is "access a webcam". Servers and containers rarely have a camera; laptops do. Requiring native V4L2 capture on the server would make the product untestable on most machines and undemoable in CI.

**Decision.** Implement four `VideoSource` implementations — `browser`, `device`, `file`, `rtsp`. The browser source receives JPEG frames pushed from the page over a WebSocket (`getUserMedia` → `canvas` → `toBlob` at target FPS). `device` (OpenCV index) exists for real deployments and edge boxes.

**Alternatives considered.** Server-side V4L2 only (untestable without hardware, breaks CI/E2E, useless in containers); WebRTC SFU (heavier, needs signalling infra, browser-compat work); WebTransport (better than WS, poor support, no benefit here).

**Consequences.**
- *Disadvantages*: frames cross the network (LAN-only by default, documented); browser is the capture clock, so the backend sees client-timestamped frames; requires a browser on the operator's machine (which is true anyway); camera permission UX lives in the browser.
- *Benefits*: zero-install webcam on any laptop; fully E2E-testable with Chromium's fake capture device and a fixture file; enables the "no hardware needed" demo; privacy property: frames go to *your own* backend, never to a third party.
- *Migration*: `VideoSource` is an interface; new transports (WebRTC, RTP, gRPC) are new implementations. A raw H.264/JPEG push endpoint can be added for edge agents without changing the pipeline.
- *Reconsider when*: if per-frame bandwidth over WAN matters (Phase 7/8) → then add a compressed push protocol; the interface is already there.

## ADR-010: WebSocket for live state, MJPEG for preview

**Context.** Two distinct real-time needs: a *structured live state* stream (boxes, tracks, zone state, health, events) and a *video preview* stream for server-side sources (file/RTSP/device).

**Decision.** WebSocket with JSON state frames (throttled, drop-oldest per client, heartbeat + timeout) for state; `multipart/x-mixed-replace` MJPEG for server-side preview, plus `<video>` for browser and HLS-compatible sources.

**Alternatives considered.** *SSE* for state (simpler, no client→server, but we need client→server for browser frames, and binary is better for images — so one protocol for both). *WebRTC* for preview (best latency and efficiency, but requires a media server dependency — rejected for now, noted as the future path). *HLS* (great for VOD, useless for sub-second live). *Base64 JPEG over WebSocket* (33% bandwidth penalty — rejected for frames; plain JSON for state is fine).

**Consequences.**
- *Disadvantages*: MJPEG is bandwidth-hungry (only used for preview, capped at low resolution and configurable FPS); two real-time paths to test; no built-in back-pressure (we implement it: single-slot latest-value buffer per client).
- *Benefits*: zero extra dependencies; works in every browser; works through any reverse proxy without config; trivially inspectable with `websocat`/`curl` for debugging.
- *Reconsider when*: preview must cross the public internet (→ WebRTC) or we need > ~10 preview viewers per camera (→ HLS/fan-out with a media server).

## ADR-011: React + Vite + Tailwind + TanStack Query

**Context.** Needs a polished, responsive, operator-grade dashboard that a *product* team can maintain, with dense live data and typed API integration.

**Decision.** React 19 + TypeScript (strict) + Vite (build/dev server) + Tailwind CSS for styling + React Router for navigation + TanStack Query for all server state. No global state library; React Context for ephemeral UI state only.

**Alternatives considered.** Next.js (SSR adds a server, unnecessary for an authenticated dashboard and complicates offline/live usage); Vue/Svelte (equally good, smaller hiring pool); plain HTMX + server templates (very cheap, but the live overlay + zone editor + dense event table would fight the paradigm); Redux/Zustand (unnecessary — server state belongs in the query cache); a component library (MUI/Ant) (large, opinionated, harder to achieve the operator-console aesthetic; Tailwind keeps the bundle tiny and the design system in-repo).

**Consequences.**
- *Disadvantages*: build-tooling surface (Vite/TS/Tailwind config to maintain); a real dependency set to audit (`react`, `react-dom`, `react-router-dom`, `@tanstack/react-query` — four runtime deps, deliberately); Tailwind class-heavy markup.
- *Benefits*: fastest path to a high-quality, responsive, accessible UI; typed contracts; excellent DX; tiny production bundle (no component library); Vitest + Testing Library for tests; Playwright for E2E.
- *Migration*: the API client is a thin typed wrapper, so swapping the query library or the router is contained to `lib/` and `main.tsx`.
- *Reconsider when*: never for these choices; revisit only if we need server-side rendering for public marketing pages (a separate marketing site, not this app).

## ADR-012: Polygon zones with foot-point containment

**Context.** V0.1 must support a "virtual restricted zone" defined by a user over live video, and decide entry/exit robustly.

**Decision.** Zones are polygons of ≥ 3 points in **normalised** coordinates (a rectangle is a 4-point polygon). Entry/exit is decided on the **foot point** = bottom-centre of the track's bounding box, using an even-odd point-in-polygon test, with hysteresis (`min_dwell_seconds`), per-zone `cooldown_seconds`, and a per-zone class filter. Directional semantics are reserved for a `line` zone kind (`NEXT`).

**Alternatives considered.** Bounding-box overlap (misses partial overlap; over-triggers when boxes are wide); mask intersection area ratio (needs segmentation or per-pixel masks — expensive and heavy at CPU scale); single reference point at box centre (mislabels leaning/short people); a dedicated segmentation model (best geometry, ~10× the CPU cost and much more complexity — `FUTURE`, available behind the same `ZoneEngine` interface).

**Consequences.**
- *Disadvantages*: a person standing with feet outside but torso inside is *not* counted (deliberate, documented); a person whose box is clipped by the frame edge gets a biased foot point (mitigated: we mark such tracks `edge_clipped` and suppress their transitions, because a clipped box has an unreliable bottom edge — an important, frequently-omitted detail); zones must be roughly ground-plane-aligned to be intuitive.
- *Benefits*: one 10-line function, exact, deterministic, trivially unit-tested, resolution-independent, and extensible to polygons/lines/masks.
- *Migration*: `ZoneEngine` with a strategy registry — adding line-crossing, distance or mask-based strategies touches one new class plus a registry entry.
- *Reconsider when*: segmentation-based masks become affordable on target hardware (then add an IoU-mask strategy and compare on measured accuracy).

## ADR-013: Events are the only durable product

**Context.** We must decide what to persist. Persisting every detection is write amplification, cost, and a privacy liability (behavioural trails). Persisting only aggregates loses evidence and auditability.

**Decision.** Persist **typed events** only. Detections and tracks are in-memory and ephemeral. Optional JPEG snapshots on event (opt-in per organisation, retention-coupled, stored as files).

**Alternatives considered.** Persist detections + tracks (high volume; needed for FUTURE analytics/AI but not now — designed in `docs/DATABASE.md` with retention); persist frames/video (privacy + storage cost — explicitly not a V0.1 feature); aggregates only (no evidence, poor auditability).

**Consequences.**
- *Disadvantages*: richer future analytics will eventually want raw detections → a Phase 3 addition with a *sampled* retention policy; debugging a historical incident has less context than a DVR.
- *Benefits:** (typo-free) excellent write profile (an event is a rare, meaningful thing), minimal privacy surface, cheap SQLite operation, and a clear answer to "what does VisionOps store?" in the privacy docs.
- *Migration*: additive — `detections`/`tracks` tables introduced in Phase 3 behind a config flag with sampling.
- *Reconsider when*: an operator asks a question only raw detections can answer (that is exactly the trigger; it is already planned).

## ADR-014: API-key authentication in V0.1

**Context.** V0.1 has one local operator. HTTP Basic auth or none would be sloppy; full OIDC/JWT/RBAC is Phase 6 work that would delay the product slice.

**Decision.** API keys: 32 random bytes, `vo_<id>_<secret>` format, SHA-256 + per-key salt at rest, constant-time comparison, per-org scoping, last-used tracking, revocable, nameable, rotatable. All permission checks are written against permission strings with a single implicit `owner` principal. In-process token-bucket rate limiting per key.

**Alternatives considered.** HTTP Basic (credentials in every request, base64, no rotation story); JWT with a self-issued secret (needs key management and a user store we do not have yet); OIDC now (external dependency, UX the single-operator case does not need); no auth + bind to localhost (fails immediately on LAN exposure).

**Consequences.**
- *Disadvantages*: no human identity in the audit trail (V0.1 audits record the key, which is honest and correct for a single-operator deployment); browser cannot hold a key safely in a way that survives reload without a prompt — mitigated by storing it in the browser only after explicit user entry, never baking it into the bundle.
- *Migration to Phase 6*: add `AuthProvider` implementations (`ApiKeyProvider` today, `OidcProvider` later), populate `Principal.user_id` from the session, and start enforcing the permission matrix. Handlers are already written against `Principal.can(...)`, so this is additive.
- *Reconsider when*: the first multi-user deployment — Phase 6. Do not build it earlier; it is a large surface (sessions, invitations, recovery, MFA) that adds risk without a user.

## ADR-015: Protocol-based pluggable vision interfaces

**Context.** The brief requires that YOLO (or any model) be replaceable without rewriting the application, and that the tracker/zone/event logic be testable without models.

**Decision.** Define narrow `typing.Protocol` interfaces — `VisionModel`, `Tracker`, `ZoneStrategy`, `EventSink`, `VideoSource` — and registries that construct implementations by name from configuration. `vision/` depends only on the standard library and NumPy.

**Alternatives considered.** Abstract base classes (imposes inheritance on implementers who may not want it); a plugin framework such as `pluggy`/entry points (real value at Phase 11 marketplace scale, real cost and a security surface now); factory functions per model type in the pipeline (`if model_type == …` chains — rejected: unmaintainable).

**Consequences.**
- *Disadvantages*: `Protocol` conformance is checked structurally and only at runtime; an alternative tracker must be written before we know it is needed (accepted: the interface is the constraint, not a marketplace).
- *Benefits*: fake models in tests are one class; swapping in a different model is a new file plus one registry line; the vision package is importable in any context (edge agent, offline eval script, notebook).
- *Migration*: entry-point-based discovery can be added *later* without changing any interface.
- *Reconsider when*: third-party community modules exist (Phase 11) — then add entry-point discovery plus a signed-module policy.

## ADR-016: In-house JSON logging and metrics registry

**Context.** Production needs structured logs and metrics; `structlog`/`prometheus-client` are the usual choices. We want few dependencies and total control over redaction.

**Decision.** A ~60-line JSON log formatter on `logging` (console renderer for dev) and a small lock-protected counters/gauges/histogram registry exported in Prometheus text format from FastAPI. No third-party telemetry libraries. Errors are logged with tracebacks; secrets are redacted by a field filter.

**Alternatives considered.** `structlog` (excellent, but adds a dependency for renderer convenience); `prometheus-client` (mature, ~fine, but adds an HTTP surface we don't need — we already have one); OpenTelemetry (heavy; `NEXT`/FUTURE via the OTLP endpoint rather than an in-process SDK); Sentry (genuinely valuable for error reporting — `FUTURE`, as an optional sink behind the logging interface, no lock-in).

**Consequences.**
- *Disadvantages:* we maintain the formatter and registry (~150 lines, tested); no automatic runtime instrumentation; histogram buckets are ours to tune.
- *Benefits:* zero dependencies, no data leaving the process by default (a privacy plus), full redaction control, Prometheus-compatible output that any scraper understands.
- *Migration:* add `structlog`/OTel/Sentry behind the logging interface if the team wants richer features — the call sites do not change.
- *Reconsider when:* we need distributed tracing across API and workers (→ OpenTelemetry) or third-party error reporting (→ Sentry). Both are Phase 6+ concerns.

## ADR-017: stdlib `cryptography` for secret encryption

**Context.** Camera credentials (RTSP user/password) and any other secret must not sit in plaintext in the database.

**Decision.** `cryptography`'s AES-256-GCM for envelope encryption of credential blobs, keyed by a master key from the environment (`VISIONOPS_MASTER_KEY`), with a version prefix and associated data binding each ciphertext to its owning row. Key rotation supported via a key id prefix.

**Alternatives considered.** `passlib`/`bcrypt` (passwords, not encryption); Fernet from `cryptography` (fine, but we need AEAD with AAD); Vault/KMS (Phase 10 for cloud; local deployments cannot depend on it); OS keyring (per-user, breaks containers and multi-user servers); do not store credentials at all (unacceptable: operators need cameras to reconnect after reboot).

**Consequences.**
- *Disadvantages*: one more dependency (`cryptography`, already ubiquitous via other packages); master key management is the operator's job (documented, with a rotation procedure); if the master key is lost, stored credentials are unrecoverable (documented explicitly — the honest behaviour, since a recoverable key would be a backdoor).
- *Benefits:* real AEAD, tamper detection, widely audited library, no bespoke crypto.
- *Migration:* KMS/Vault in cloud mode via a `KeyProvider` interface — one class to implement.
- *Reconsider when*: cloud mode with a managed KMS (Phase 10); never to roll our own crypto.

## ADR-018: No facial recognition, ever

**Context.** The brief asks for privacy-preserving analytics and explicitly says to avoid making facial recognition a core dependency. Beyond the brief, face recognition creates legal exposure (GDPR special-category data, NDPA Nigeria, biometric regulation), a bias surface, and an abuse surface.

**Decision.** VisionOps will **not** ship, depend on, or support face detection, face recognition, person re-identification across sessions/cameras, or any biometric identity. The schema contains no identity fields; tracks are ephemeral session-scoped integers; no model in this repository performs face analysis. Operational analytics are over objects, anonymous track IDs, zones, counts and timestamps.

**Alternatives considered.** Optional face-blur *detection* for privacy (interesting, adds a model and a biometric-adjacent dependency — deferred as `FUTURE` blur-on-capture using a generic person segmenter, documented as an opt-in privacy feature, never an identity feature); customer-supplied identity models (out of scope; would make VisionOps a surveillance-identity platform).

**Consequences.**
- *Disadvantages*: some buyers will ask for identity-based workflows; we must manage that expectation with clear positioning (occupancy and compliance, not identification).
- *Benefits:* the strongest possible privacy posture, minimal regulatory surface, smaller model downloads, faster inference, and a defensible public position.
- *Migration:* n/a — this is a permanent product constraint. Any proposal to add it requires a new product decision reviewed at the highest level, not a code change.
- *Reconsider when:* never.

## ADR-019: Normalised coordinates everywhere

**Context.** Cameras have different resolutions, aspect ratios and crop modes; model inputs are fixed-size letterboxed tensors; users draw zones on a scaled UI element. If pixels leak into the data model, every future feature (multi-camera, resolution changes, model input-size changes, DB migrations) breaks.

**Decision.** All geometry that crosses a boundary — detections, tracks, zone polygons, event metadata, WebSocket payloads, database rows — is **normalised `[0,1]`** relative to the source frame, with the aspect ratio preserved (no stretching). Conversions to pixels happen only at render time, in the UI, using the displayed element's `clientWidth/Height`.

**Alternatives considered.** Pixels with per-camera resolution metadata (breaks the moment a camera changes resolution, and mixes presentation with data); normalised to a fixed 1000-unit grid (arbitrary precision); normalised with separate x/y scaling (breaks aspect ratio for non-square frames — avoided).

**Consequences.**
- *Disadvantages:* every consumer must remember to multiply by dimensions (mitigated by a single `denormalize()` helper in the UI and by convention that `vision/` never emits pixels); precision loss is negligible for these magnitudes.
- *Benefits:* resolution- and model-input-independent data; zones survive a resolution change; the UI scales for free; multi-camera and multi-site analytics become trivial; storage is compact.
- *Migration:* none needed — this is the schema from day one. Introducing pixel storage later would be a breaking change, which is exactly why we do not.
- *Reconsider when:* never; it is the load-bearing decision for future scale.

## ADR-020: Model download on first run, with offline mode

**Context.** A great first-run experience needs the model to "just be there", but air-gapped and bandwidth-constrained deployments must never be forced to download 11 MB from the internet.

**Decision.** On startup, if no model is present at the configured path, attempt a download from a configurable URL to a cache directory, verify the file is a loadable ONNX model, and record its SHA-256. Offline deployments set `VISIONOPS_MODEL_PATH` (or disable auto-download) and get an explicit, visible `AI UNAVAILABLE` state in System Status — never a silent failure.

**Alternatives considered.** Bundle weights in the image (image bloat, licence review per release, slow pulls); require manual download always (worse first-run experience); use a model registry service (Phase 9).

**Consequences.**
- *Disadvantages:* first run needs internet; download failure must be handled gracefully (it is: degraded state + actionable message + documented manual placement).
- *Benefits:* zero-config first run; same weights everywhere; checksum recorded for reproducibility.
- *Migration:* model registry (Phase 9) replaces the URL with a versioned artefact lookup; checksum verification stays.
- *Reconsider when*: multi-model deployments or air-gapped enterprises (Phase 7).

## ADR-021: Commitments to dependencies

**Context.** Dependency creep is the main long-term cost in a project like this, and every dependency is also a security surface and a supply-chain risk.

**Decision.** Add a dependency only when it is (a) already in the transitive set, (b) solving a problem we would otherwise solve worse ourselves, or (c) explicitly required for correctness in a domain we should not re-enter (e.g. ONNX inference). Default to the standard library. Every dependency in `pyproject.toml`/`package.json` has a comment-free but documented rationale in this ADR, a pinned version range, and a removal plan.

**Current runtime dependency budget:**

*Python (9):* `fastapi`, `uvicorn[standard]`, `pydantic`, `pydantic-settings`, `sqlalchemy`, `alembic`, `numpy`, `opencv-python-headless`, `onnxruntime`.
*TypeScript (4):* `react`, `react-dom`, `react-router-dom`, `@tanstack/react-query`.
*Dev:* `pytest`, `pytest-cov`, `httpx`, `ruff`, `mypy`, `vite`, `typescript`, `vitest`, `@testing-library/*`, `eslint`, `playwright`, `tailwindcss`.

**Explicitly rejected dependencies:** `ultralytics`/`torch` at runtime (ADR-006 — dev-only extra), `celery`/`redis` (ADR-025), `structlog`/`prometheus-client` (ADR-016), `passlib` (ADR-017), `pydantic-settings` alternatives (config is simple enough), `celery-beat`/APScheduler (a single `visionops prune` command plus cron/systemd timer is more robust and simpler), `shapely` (zone maths is one even-odd test), `opencv-python` GUI variant (`-headless` — we render in the browser), any auth/JWT library for V0.1 (ADR-014).

**Consequences.**
- *Disadvantages:* we write more code ourselves (logging, metrics, tracker, NMS) — more code to test and own, which is exactly the trade we accept.
- *Migration:* an internal utility replaced by a library later is a local refactor.
- *Reconsider when:* a dependency's maintenance status deteriorates, or it acquires a CVE with no fix.

## ADR-022: Testing strategy — scripted detectors and fixtures, not hardware

**Context.** "The project must remain testable without physical camera hardware" is a hard requirement, and CI machines never have cameras.

**Decision.** Three complementary test layers: (1) **deterministic logic tests** using a `FakeDetector`/scripted `VisionModel` that emits exact detections per frame, so tracker, zone and event logic is asserted exactly; (2) **pipeline tests** using a committed synthetic fixture video, so decode, rate control, lifecycle and failure handling are exercised; (3) **real-model tests** gated behind `@pytest.mark.model`, **skipped** (never faked) when weights are absent. E2E uses Chromium's fake capture device with a fixture video.

**Alternatives considered.** Record-and-replay of real footage (privacy/licensing issues, large binaries, non-determinism across encoder versions); mocking OpenCV entirely (tests the mock, not the code); requiring hardware in CI (unacceptable).

**Consequences.**
- *Disadvantages:* real-model accuracy is not covered by default CI (mitigated: the marker-gated suite plus an opt-in CI job with cached weights, and documented local runs); synthetic fixtures do not represent real optical conditions (mitigated: labelled clearly as synthetic, used only for plumbing/logic, never for accuracy claims).
- *Benefits:* 100% deterministic, fast, offline, hardware-free, and the exact assertions we want on the logic that actually produces customer events.
- *Migration:* if we adopt a benchmark harness, keep the fake-detector tests as the regression net — they stay valid regardless of model.
- *Reconsider when:* never; hardware-free testing is permanent policy.

## ADR-023: Makefile as the single task entrypoint

**Context.** Two toolchains (uv, npm) plus Alembic, Alembic-free scripts, Playwright and Docker must be run consistently by humans and by AI agents on any OS.

**Decision.** A root `Makefile` is the canonical interface (`make install`, `make dev`, `make test`, `make lint`, `make typecheck`, `make build`, `make migrate`, `make prune`, `make bench`, `make demo`, `make clean`). `uv run` and `npm run` remain available for narrow tasks. CI calls `make` targets so CI and humans run identical commands.

**Alternatives considered.** npm workspaces + `concurrently` (mixing JS tooling to orchestrate Python is backwards); Taskfile (extra dependency); shell scripts everywhere (no discoverability, no portability); Docker Compose for dev (excluded: Docker must not be required — many target machines do not have it).

**Consequences.**
- *Disadvantages:* `make` on Windows without WSL; slight indirection for one-off commands.
- *Benefits:* one discoverable entrypoint (`make help`), identical commands for humans, agents and CI, no extra tooling.
- *Migration:* none.
- *Reconsider when:* never.

## ADR-024: Time-ordered UUIDs for events

**Context.** Events will eventually be created on edge nodes and replicated to a central store (Phase 7), and must be deduplicated on sync. Sequential integers are simpler but collide across nodes and leak volume. Random UUIDv4 sorts randomly, which fragments indexes.

**Decision.** Time-ordered UUIDs (UUIDv7-style: 48-bit millisecond timestamp + random bits), generated by our own `core/ids.py`, used as primary keys for all domain tables.

**Alternatives considered.** Auto-increment integers (simple, but cross-node generation and volume leakage); UUIDv4 (no ordering, index fragmentation); ULID (same idea, extra dependency — rejected, ~40 lines of code is cheaper).

**Consequences.**
- *Disadvantages:* IDs are not opaque (they embed a timestamp — acceptable for operational data, and we never treat them as secrets); a 4-line correctness risk in the generator (covered by unit tests asserting monotonicity and uniqueness).
- *Migration:* none.
- *Reconsider when:* never; this is also what makes future edge sync idempotent for free.

## ADR-025: No message broker in V0.1

**Context.** The event flow (pipeline → store → hub → UI → future notifications) looks like it wants a queue. The brief forbids unnecessary brokers.

**Decision.** Direct function calls through an `EventSink` interface. Persistence is synchronous and happens *before* broadcast; broadcast is best-effort. Concurrency is bounded by a session cap and per-client drop-oldest buffers.

**Alternatives considered.** Redis Streams/pubsub (Phase 6+, when multiple processes exist); RabbitMQ/Kafka (Phase 10, at SaaS scale, and only if replay semantics become a requirement); Celery (rejected: heavy, and the only task in V0.1 is a scheduled prune which cron does better).

**Consequences.**
- *Disadvantages:* no durable pub/sub, so a crashed broadcast is recovered only by clients re-fetching over REST (which is why REST history is authoritative and the socket is ephemeral); single-node fan-out only.
- *Benefits:* zero operational dependencies, no data-residency surface, lowest latency, simplest possible failure model.
- *Migration:* `EventSink` implementations for Redis/Kafka are additive and behind the same interface; ordering guarantees (single-writer per session) already make delivery semantics easy to reason about.
- *Reconsider when:* a second process needs to receive events (Phase 6), or replay-from-offset becomes a requirement (Phase 10).

---

## Appendix A — Rejected-for-now technology watchlist

Recorded so future contributors do not re-litigate them without new information:

| Technology | Why not now | Trigger to revisit |
| --- | --- | --- |
| Redis / Celery | No second process yet (ADR-025) | Phase 6 multi-process |
| PostgreSQL-only features (RLS, partitioning) | SQLite-first promise (ADR-005) | Phase 6 (RLS), Phase 3 (partitioning) |
| Kubernetes / Helm | Overkill for single-node; cloud-specific | Phase 10 |
| Terraform/Pulumi | No cloud target chosen; premature | Phase 10 with a real customer cloud |
| Prometheus/Grafana as a *requirement* | We export Prometheus format; running the stack is the customer's choice | When we run managed SaaS |
| Vector database | Structured events + aggregates answer the questions; AI agent can use SQL | Phase 9, if semantic search over event metadata proves necessary |
| Kafka | Massive operational cost for a product whose event rate is single digits per second | Never, probably |
| Kubernetes-native CV operators | Wrong altitude | Never |
| WebRTC SFU | Real benefit for WAN preview, real complexity | Phase 8 if customers demand remote low-latency video |
| Sentry / Datadog | Useful, and must be optional | First paid customer needing managed observability |
| Elasticsearch | Events are queryable in SQL with the right indexes | Never, probably |
| Feature store / MLflow | We do not train production models in production (ADR-006) | Phase 9 model evaluation at scale |
