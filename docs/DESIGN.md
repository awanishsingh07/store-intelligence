# DESIGN.md — Store Intelligence API

## Architecture Overview

The system is split into two independent components that communicate via HTTP:

**Detection pipeline** (`pipeline/`) runs offline against the CCTV clip files.
It uses YOLOv8n for person detection, ByteTrack for multi-object tracking, and
rule-based zone assignment from `store_layout.json`. Output is a `.jsonl` file
of structured events which are then batch-posted to the API.

**Intelligence API** (`app/`) is a FastAPI service backed by SQLite (WAL mode).
It ingests events, persists them, computes analytics on demand, and exposes six
endpoints. There is no message queue or streaming layer — events flow from the
pipeline as HTTP batches. This keeps the architecture simple and eliminates
infrastructure dependencies that would add operational complexity for no gain
given the dataset size.

```
CCTV clips → pipeline/detect.py → events.jsonl → POST /events/ingest → SQLite
                                                                            ↓
                                               GET /metrics, /funnel, /heatmap, /anomalies, /health
```

### Component breakdown

| Component | Responsibility |
|---|---|
| `pipeline/detect.py` | Frame extraction, YOLO inference, ByteTrack, event emission |
| `pipeline/tracker.py` | Re-ID logic, trajectory similarity, REENTRY detection |
| `pipeline/emit.py` | Event schema construction and validation before posting |
| `app/routers/` | HTTP layer — request/response only, no business logic |
| `app/services/` | All business logic — metrics, funnel, heatmap, anomalies |
| `app/models/` | SQLAlchemy ORM — Event, StoreBaseline |
| `app/core/` | Config, DB setup, logging middleware, error handlers |

### Storage

SQLite with WAL journal mode. Chosen over PostgreSQL because:
1. The dataset is bounded (5 stores × ~1h footage = thousands of events, not millions)
2. WAL mode provides adequate concurrent read performance for this scale
3. Zero infrastructure — the DB is a file in a Docker volume, no second container needed
4. Simplifies the acceptance gate: `docker compose up` starts exactly one service

Indexes on `(store_id, timestamp)`, `(store_id, visitor_id)`, and
`(store_id, zone_id, timestamp)` cover every analytics query pattern.

---

## AI-Assisted Decisions

### 1. ByteTrack vs StrongSORT for multi-object tracking

I asked Claude to compare ByteTrack, StrongSORT, and DeepSORT for this use case.
The key tradeoff: ByteTrack is faster and already built into the `ultralytics` package
(zero extra setup), while StrongSORT adds appearance features (OSNet embedding) that
improve Re-ID accuracy at the cost of a separate model download and ~2× inference time.

**AI suggested**: StrongSORT for better Re-ID accuracy given the re-entry requirement.

**My decision**: ByteTrack. The 20-minute clips don't have the scale where StrongSORT's
appearance model pays off over IoU-based matching. Re-ID accuracy is supplemented by
the trajectory similarity logic in `tracker.py`. If accuracy is insufficient after
testing, upgrading to StrongSORT is a one-line change to the tracker config.

### 2. Rule-based zone classification vs VLM

I asked Claude whether a VLM (Claude Vision / GPT-4V) would outperform rule-based
zone assignment using bounding box centroid against `store_layout.json` polygons.

**AI suggested**: VLM could handle ambiguous zone boundaries and shelf-obscured cases.

**My decision**: Rule-based. The store_layout.json already contains explicit zone
polygons with camera coverage. Centroid-in-polygon is deterministic, fast, and
produces no API costs or latency. VLM classification would be slower, non-deterministic,
and harder to debug. The CHOICES.md entry on this captures the tradeoffs in detail.

### 3. Session deduplication for the funnel endpoint

I asked Claude how to handle re-entry in the session funnel — whether a visitor who
exits and re-enters should count as a new session or the same one.

**AI suggested**: Treat each ENTRY event as a new session (simpler, more conservative).

**My decision**: Same session if a REENTRY event links the visitor_id. The business
problem is "unique visitors who purchased" — inflating sessions by counting re-entries
as new visitors would inflate the denominator and understate conversion rate. This
matches the challenge spec's note that "re-entry inflation is a known vendor problem
you are solving."

---

*Last updated: [date of submission]*
