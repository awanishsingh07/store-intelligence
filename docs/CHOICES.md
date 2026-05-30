# CHOICES.md — Architecture Decision Record

## Decision 1: Detection model — YOLOv8n + ByteTrack

### Options considered
- **YOLOv8n** (nano) — fastest, lowest memory, ~80% mAP on COCO person class
- **YOLOv8x** (extra-large) — highest accuracy, ~4× slower, requires GPU with >8GB VRAM
- **RT-DETR** — transformer-based, slightly better on crowded scenes, harder to set up
- **MediaPipe** — good for single-person, degrades with crowds, no ByteTrack integration

### What AI suggested
Claude suggested YOLOv8x for maximum accuracy given that the challenge scores entry/exit
count accuracy against ground truth. It noted that the extra inference time is acceptable
for offline batch processing.

### What I chose and why
**YOLOv8n**. The clips are 1080p at 15fps. On a laptop CPU, YOLOv8x processes ~0.3fps
(20 min clip takes hours). YOLOv8n processes ~3fps on CPU, making the full dataset
tractable in ~2 hours. The accuracy gap at this resolution and person-detection task
(not fine-grained classification) is smaller than the benchmark numbers suggest —
people at retail CCTV distances are large in frame and well-lit.

If a GPU is available, I would switch to YOLOv8m or YOLOv8l. The model is a one-line
config change in `pipeline/detect.py`.

**ByteTrack** is bundled with ultralytics, requires no additional dependencies, and
performs well on the multi-person retail scenario. The alternative (StrongSORT with
OSNet Re-ID) is evaluated in DESIGN.md — the accuracy improvement does not justify the
setup complexity given the dataset scale.

---

## Decision 2: Event schema design

### Options considered
- **Flat schema** — all fields at the top level, no nested metadata object
- **Nested metadata** — top-level required fields + `metadata` object for event-type-specific data (the spec's schema)
- **Typed union** — separate Pydantic models per event type (e.g., `EntryEvent`, `ZoneDwellEvent`)

### What AI suggested
Claude suggested typed unions (discriminated union in Pydantic v2) for stronger
validation — each event type gets its own model with exactly the fields it needs,
making invalid states unrepresentable.

### What I chose and why
**The spec's nested metadata schema**. The challenge provides an explicit required
schema with a `metadata` object. Deviating to typed unions would require translation
on every ingest call and would break compatibility with `sample_events.jsonl` and
`assertions.py`. Pydantic cross-field validators on the single schema achieve the
same safety guarantees the discriminated union would provide (e.g., `BILLING_QUEUE_JOIN`
requires `metadata.queue_depth`, `ZONE_ENTER` requires `zone_id`).

The `confidence` field is never suppressed or rounded — low-confidence events go
into the DB at their raw value. The `is_staff` flag is stored and filtered at query
time, never at ingest time.

---

## Decision 3: API architecture — FastAPI + SQLite, single container

### Options considered
- **FastAPI + SQLite, single container** — chosen
- **FastAPI + PostgreSQL, two containers** — more production-like, better concurrency
- **FastAPI + Redis + PostgreSQL** — full production stack with event queue
- **Flask + SQLite** — simpler framework, worse async support

### What AI suggested
Claude suggested FastAPI + PostgreSQL for production correctness, noting that SQLite
has write serialisation issues under concurrent load and doesn't support true
parallel writes.

### What I chose and why
**FastAPI + SQLite in WAL mode, single container**. The concurrency argument for
PostgreSQL assumes multiple writers hitting the DB simultaneously. In this system,
the only writer is the `/events/ingest` endpoint, which is called in sequential
batches from the pipeline. WAL mode handles this cleanly. Readers (`/metrics`,
`/funnel`, etc.) can run concurrently with the writer in WAL mode.

The single-container approach directly satisfies the acceptance gate requirement:
`docker compose up` starts everything with no manual steps. Adding PostgreSQL would
require coordinating startup order, health checks, and migrations across two containers —
operational complexity with no benefit at this data scale.

FastAPI is chosen over Flask for native async support (SQLAlchemy async sessions,
`async def` handlers) and built-in Pydantic v2 integration.

---

*Decisions recorded in real time during implementation.*
