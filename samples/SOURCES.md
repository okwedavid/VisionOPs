# Sample assets and their provenance

Every asset in this directory is listed here with its origin and licence.
Nothing is committed without a row in this table.

## `fixtures/zone-walkthrough.mp4`

| Field | Value |
| --- | --- |
| Kind | **SYNTHETIC — drawn, not filmed** |
| Generator | `scripts/make_fixture.py` (committed, deterministic, no random seed) |
| Contents | Simple geometric figures walking left-to-right through a drawn rectangle marked "restricted" |
| Contains real people | **No.** There are no people, faces or real scenes in this clip |
| Resolution / length | 320×240, 10 fps, 120 frames (12 s), ~106 KB |
| Licence | Same as the repository (MIT) — it is our own output |
| Regenerate with | `python scripts/make_fixture.py` |

**What this fixture is for:** exercising the whole pipeline — decode, rate control,
tracking, zone logic, event generation, persistence, the live overlay and the
browser UI — on a machine with no camera, no GPU and no network.

**What this fixture is NOT for:** demonstrating detection accuracy. A drawn
rectangle is not a person, and VisionOps will (correctly) not detect one. Accuracy
is measured per deployment with a documented methodology, never claimed from a
synthetic clip.

## Rules for adding assets

1. **No footage of identifiable people** without documented, explicit consent
   from everyone visible, plus a signed release.
2. Prefer **CC0 / CC-BY / public-domain** footage, and record the exact URL,
   licence text and retrieval date.
3. Keep files small enough to keep the repository usable: a 12-second 320×240
   clip is acceptable, a 200 MB sample dump is not.
4. Note anything synthetic explicitly, and say what it does and does not prove.
5. Never commit a recording taken from a customer's deployment.

## No people datasets

VisionOps ships **no** dataset of people, faces or biometrics (ADR-018, Phase 9).
Labelled data stays customer-owned; see `docs/PRD.md` §34.
