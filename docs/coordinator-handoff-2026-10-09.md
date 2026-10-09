# AudiobookFunnel coordinator handoff

Verified on **October 9, 2026**, from the repository, GitHub release/CI records, and limited deployment checks. Recommendations are distinguished from existing behavior. This document excludes credentials, private logs, and personal library records.

## 1. Current version and implemented scope

- **Branch:** `main`
- **Inspected commit:** `534c8b7bd99fc159a4858be2c1806ed705882d1a`
- **GitHub main:** matched the local commit at inspection.
- **Declared package version:** `0.1.0`
- **Formal releases:** no Git tags or GitHub releases. The latest usable build is the inspected commit; there is no separately versioned release artifact.
- **Working tree at inspection:** tracked `.env.example` was locally deleted. That deletion was uncommitted.

**Implemented:** Dockerized ingest, persistent jobs/settings, grouping and ordering, metadata search/editing, explainable confidence scoring, configurable ignored title terms, manual cover URL/upload, M4B processing, sidecars, final naming/publication, error dismissal/recovery, destination-collision handling, qBittorrent submission and post-seeding archiving/confirmed clearing, and ABS scan requests.

**Planned or absent:** dedicated side-by-side review inbox, cross-app request tracking, ABS item reconciliation, resumable encodes, archive extraction, active cancellation, automatic work-folder cleanup, cross-package merging, authentication, and comprehensive database migrations.

## 2. Architecture, models, and persistence

FastAPI serves the browser UI and JSON API. One background worker runs discovery, inspection, finalization, ABS scanning, and download archiving. SQLite is authoritative; folder placement does not define job state.

| Component | Purpose |
|---|---|
| `app/main.py` | API, review/edit/approval actions, settings |
| `app/worker.py` | Single-worker processing loop and integration scheduling |
| `app/media.py` | ffprobe inspection, staging, FFmpeg processing, tags, publication |
| `app/metadata.py` | Provider adapters, title filtering, scoring |
| `app/archive.py` | Persistent qBittorrent move/delete reconciliation |
| `app/covers.py` | Cover retrieval, validation, normalization |
| `app/static/` | Browser UI |

Entry points are `uvicorn app.main:app` and `python -m app.worker`. Compose exposes the UI on localhost port `8095`; API documentation is at `/docs`.

Persistent tables:

- `packages`: unique source path, inspection status.
- `jobs`: UUID, package association, status, JSON body, error, output directory.
- `settings`: validated configuration.
- `events`: transition/audit records.
- `torrents`: submitted `.torrent` file digests.
- `scan_requests`: independent ABS scan queue.
- `archives`: cleanup state keyed by actual torrent hash.
- `runtime`: worker heartbeat and operational markers.

Job JSON includes source-file inspection data, embedded/editable metadata, candidates, provenance, grouping confirmation, cover choice, and a frozen publication plan.

**Storage:** Compose binds repository `./data/config` to `/config`; SQLite lives at `/config/funnel.db`, manual covers beneath `/config/covers`. `/work` holds private per-job working files. `/library` holds published outputs. `/source`, `/torrents`, and `/archive` are mounted read-only to Funnel.

## 3. Existing integrations and contracts

### Audiobookshelf

The only implemented ABS API operation is:

```text
POST /api/libraries/{configured_library_id}/scan
```

Successful publication queues a durable scan request. Requests wait for the runnable finalization batch to drain. Failures retry independently, approximately every 60 seconds when the worker is available.

`ACCEPTED` means the HTTP request succeeded—not that indexing finished. There is no ABS item lookup, metadata synchronization, webhook, playback synchronization, or item-ID storage.

Deployment check on October 9: **zero accepted scan records and one pending request**. Successful live ABS scanning was therefore not established.

### BookRamp and AutomaticBookSelector

No named integration, request identifier, completion callback, webhook, shared schema, or dedicated handoff document exists in this repository. Neither other repository was inspected for this handoff, so their independent capabilities remain unknown.

### Other existing integrations

- Audible catalog: `GET /1.0/catalog/products`, or `/1.0/catalog/products/{ASIN}`; title/author search and Audible URL-to-ASIN extraction.
- Google Books: `GET /books/v1/volumes`.
- Open Library: `GET /search.json`.
- qBittorrent: login, torrent upload, torrent/file/preference inspection, `setLocation`, and confirmed `delete` with `deleteFiles=true`.

The acquisition input contract is presently **completed audio files/folders** and optionally standard **`.torrent` files** in the configured inbox. There is no cross-app acquisition-request JSON format.

Useful Funnel APIs include `GET /api/jobs`, `GET /api/jobs/{id}`, `POST /api/discover`, and job search/edit/select/approve/recovery actions. These are local application APIs, without a versioned integration contract or authentication.

## 4. Recommended ownership boundaries

**Recommendation:** Funnel should own the conversion of a completed source package into verified, reviewed, library-ready audiobook jobs: grouping, edition matching, metadata, artwork, processing, publication, and its own processing status.

It should **not** own:

- Recommendation/ranking or deciding which books to acquire.
- A shared catalog or acquisition-request lifecycle without an agreed contract.
- ABS playback, user progress, or ABS's internal item identity.
- Torrent transport, seeding policy, or direct modification of seeding files.

Its existing archive feature should remain narrowly delegated through qBittorrent, with completion/seeding checks and separate deletion confirmation.

Assigning the first two responsibilities to BookRamp versus AutomaticBookSelector requires reviewing those projects.

## 5. Verification and acceptance

Commands, run from the repository root:

```powershell
.\.venv\Scripts\python -m pip install -r requirements.lock
.\.venv\Scripts\python -m pip install -e .
.\.venv\Scripts\python -m pytest -q
node --check app/static/app.js
docker compose build
docker compose up -d --no-build
```

FFmpeg and ffprobe must be on PATH for host media tests. Avoid recreating the worker during an active encode unless accepting a restart of that work.

**Verified results:**

- October 9: **111 tests passed** on Windows; JavaScript syntax check passed.
- GitHub CI: **successful for the inspected commit**.
- October 7 verification: **111 tests passed inside Docker** and Compose build succeeded.
- October 9: web healthy, worker running, configurable title-filter capability deployed.

Tests cover real generated-audio processing, chapters, covers, publication/checksums, source preservation, scoring, settings, recovery, and mocked integrations.

**Acceptance still pending:** successful live ABS scan/indexing, author/narrator interpretation in the actual ABS scanner, any cross-app lifecycle, and deployment-specific qBittorrent cleanup acceptance. Historical documentation does not establish those as completed.

## 6. Known issues, risks, and decisions

- **Author/narrator issue reported by the user:** verified output mapping places the narrator in Artist. This plausibly explains narrators appearing as authors; ABS's exact interpretation has not been verified. Treat correction and regression coverage as priority follow-up. No correction was implemented as part of this handoff.
- ABS scans remain pending; indexing completion is never monitored.
- Duplicate detection identifies an occupied destination, not equivalent recordings elsewhere.
- Discovery deduplicates by source-package path; changed content at an existing path is not a fresh import.
- Interrupted encodes restart; work/staging files can accumulate.
- One worker only; no horizontal scaling.
- Local SQLite contains integration secrets. The service has no authentication and should remain local.
- Audible is unofficial; provider availability and fallback-book edition accuracy remain limitations.
- `docs/validation.md` describes the initial 15-test/demo deployment and is stale. Architecture documentation also contains older statements alongside later archive behavior.

**Decisions awaiting the user:** cross-app ownership, shared request identity, edition/duplicate semantics, retention policy, and whether correcting existing library tags belongs in a separate controlled repair operation.

## 7. Documentation and handoffs

- [README](https://github.com/rickycatto2/AudiobookFunnel/blob/534c8b7/README.md): setup, behavior, operational limits.
- [Architecture](https://github.com/rickycatto2/AudiobookFunnel/blob/534c8b7/docs/architecture.md): safety/state contract; partly stale.
- [Roadmap](https://github.com/rickycatto2/AudiobookFunnel/blob/534c8b7/docs/roadmap.md): planned review inbox.
- [Validation](https://github.com/rickycatto2/AudiobookFunnel/blob/534c8b7/docs/validation.md): historical initial acceptance.
- [Compose](https://github.com/rickycatto2/AudiobookFunnel/blob/534c8b7/compose.yaml): services and persistence.
- [CI workflow](https://github.com/rickycatto2/AudiobookFunnel/blob/534c8b7/.github/workflows/test.yml): reproducible test setup.

**No tracked handoff document for either other project was found at inspection.** This document is the subsequently requested AudiobookFunnel coordinator handoff.

## 8. Smallest safe next integration step — recommendation only

Have the coordinator produce a **read-only completion inventory** from existing Funnel job records and final manifests:

```text
funnel_job_id
status
library-relative directory and audio filename
output SHA-256
ASIN/ISBN when present
```

Do not automate acquisition, approval, deletion, or claim ABS import completion yet. First validate one completed book across the filesystem and ABS, and agree how an upstream request will eventually map to a Funnel job. Correct the contributor tags before expanding automated imports.

This step has not been implemented.

## 9. Final M4B identity and ABS correlation

Each completed job produces one named M4B, optional `cover.jpg/png`, optional `desc.txt` and `reader.html`, and `funnel.json`.

Current M4B mapping in [the writer](https://github.com/rickycatto2/AudiobookFunnel/blob/534c8b7/app/media.py#L169):

| Atom | Current value |
|---|---|
| `©nam`, `©alb` | Title |
| `aART` | Author |
| `©ART` | **Narrator**, falling back to author |
| `©wrt` | Narrator |
| `©day`, `©gen`, `desc`, `cprt` | Year, genre, description, copyright |
| iTunes freeform fields | Series, SERIES-PART, publisher, ASIN, ISBN, language |
| `covr` | Selected artwork |
| `stik` | Audiobook marker |

Author/narrator collections are stored as joined strings, rather than structured contributor identities.

`funnel.json` contains exactly these top-level fields:

```text
job_id, metadata, provenance, files, sha256, audio_mode
```

**There is no reliable implemented correlation to the eventual ABS item ID.** The Funnel UUID is in the manifest/database, not embedded as a dedicated M4B identity field. ABS scan records are library-level and have no job linkage. ASIN is optional and identifies an edition, not an ABS instance; titles and paths can change.

The UUID plus output checksum reliably identifies Funnel's published artifact. A future reconciliation step must establish and persist its mapping to an ABS item—without assuming title or ASIN alone proves the match.
