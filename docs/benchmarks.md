# Benchmarks

`CURRENT` — measured on the development host, not a claim about every deployment.

The performance table in `docs/VISION_ENGINE.md` §10 is a **budget**. This file
records what was actually observed, on what hardware, so the budget can be
compared against reality instead of trusted.

## Methodology

- **Input:** `samples/fixtures/zone-walkthrough.mp4`, 320×240, first 40 frames.
  A *synthetic* fixture (see `samples/SOURCES.md`) — it measures the machine,
  not detection accuracy.
- **Model:** `yolo11n.onnx`, SHA-256 `634279b40c07c63…c0c053b`, 640×640 input.
- **Runtime:** ONNX Runtime 1.30.0, `CPUExecutionProvider`, `VISIONOPS_INTER_AREA=0`.
- **Timing:** `time.perf_counter`, median of 40 samples after one warm-up frame.
  The first inference is excluded: it includes graph optimisation and arena
  allocation and is typically 2–4× slower.
- **Reproduction:** `uv run visionops bench --frames 20 --file samples/fixtures/zone-walkthrough.mp4`
  (add `--output bench.json` to keep the machine-readable result). The file pass
  runs with rate control disabled so every frame reaches the model — it measures
  the host ceiling, not a deployment setting.

## Host

| Property | Value |
| --- | --- |
| CPU | 4 vCPU, x86-64, no AVX-512 reported by ONNX Runtime |
| RAM | ~7.6 GB |
| GPU | **none** — CPU inference only |
| OS | Linux 5.x, Python 3.12 |

This is a small VM. A desktop CPU of the same vintage is typically 2–4× faster
per core-count-matched, and a GPU changes the picture entirely (see *Levers*).

## Per-stage cost

| Stage | Budget (VISION_ENGINE §10) | Measured | Note |
| --- | --- | --- | --- |
| Decode (JPEG, browser path) | ≤ 25 ms | **0.30 ms** | 320×240 fixture; a 1080p stream costs ~4× more |
| Letterbox + pack | ≤ 5 ms | **3.14 ms** | 320×240 → 640×640, `INTER_LINEAR` |
| ONNX inference | ≤ 90 ms | **106.3 ms** | **over budget** on this host |
| NMS + filter | ≤ 2 ms | **< 0.01 ms** | 0 candidate boxes in the fixture |
| Track update | ≤ 1 ms | *not isolated* | included in end-to-end below |
| Zone evaluate | ≤ 1 ms | *not isolated* | included in end-to-end below |
| Event write + publish | ≤ 15 ms | *not isolated* | idle in the benchmark |
| **Total per frame** | **≤ 140 ms** | **109.7 ms** | → **9.1 fps** |

## End-to-end throughput

Full `VisionPipeline.step()` over the 120-frame fixture, rate control disabled so
every frame is inferred. From `visionops bench --file`:

| ORT threads | ms/frame | fps |
| --- | --- | --- |
| default (all cores) | 122.8 | 8.1 |
| 2 | 98.7 | **10.1** |
| 4 | 103.5 | 9.7 |
| default, whole 120-frame fixture | 102.9 | 9.72 |

The tool's own summary on this host: `inference_ms.p50` 99.9 ms,
`inference_ms.p95` 114.6 ms, `warmup_ms` 123.9 ms, `resident_memory_bytes`
≈ 228 MB, `single_stream_fps_ceiling` ≈ 9.7–10.6, `meets_target_fps` true at the
default 5 fps target.

**Observation:** on a 4-vCPU VM, oversubscribing threads is *slower* than
setting them to 2. ONNX Runtime's default (one thread per core, here 4) spends
more time in thread synchronisation than it saves. `VISIONOPS_ORT_THREADS=2` is
the fastest setting measured on this host — but that is a property of this host,
not a default worth shipping. The shipped default is unset (ORT decides).

## What this means

- The documented budget (140 ms/frame, ≥ 7 fps) is **met end-to-end** at
  109.7 ms, but **inference alone exceeds its 90 ms line** on this VM.
- The default `target_fps` of 30 is **not achievable here**; at 10 fps a session
  is comfortably real-time for zone entry/exit, which is what the product does.
  A camera set to 30 fps will be read at ~9–10 fps and the UI shows the true
  `processed_fps`, so the operator can see it rather than being misled.
- **Single-session only.** Two concurrent sessions on 4 vCPU split the same
  cores and each drops to roughly half the rate. `VISIONOPS_MAX_SESSIONS`
  defaults to 1 for this reason.

## Levers, in the order an operator should try them

1. `VISIONOPS_TARGET_FPS` — lower it. Halving it halves the load; the event
   logic does not need 30 fps to notice someone walking through a door.
2. `VISIONOPS_ORT_THREADS` — try 2 on a 4-vCPU host (measured above). Never
   higher than the core count.
3. `VISIONOPS_INPUT_WIDTH` / `INPUT_HEIGHT` — 512 instead of 640 roughly halves
   the inference cost; accuracy drops, so re-check zones afterwards.
4. `VISIONOPS_INTER_AREA=1` — better downscaling quality when shrinking a large
   frame, at a small CPU cost.
5. GPU provider (`VISIONOPS_ORT_PROVIDER=CUDAExecutionProvider`) — the only lever
   that changes the order of magnitude. Requires `onnxruntime-gpu` and a working
   CUDA setup on the host; see `docs/DEPLOYMENT.md`.

## What is deliberately *not* claimed

- **No accuracy or mAP figure.** Accuracy depends on the camera, the scene and
  the lighting; it is measured per deployment with a documented method, per
  `docs/TESTING.md`. A synthetic clip cannot measure it.
- **No multi-session or multi-camera scaling claim.** Only one session was run.
- **No comparison against other runtimes.** ONNX Runtime was not benchmarked
  against TensorRT, OpenVINO or a GPU provider on this host.
