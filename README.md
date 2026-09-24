# AudiobookFunnel

A local, Dockerized audiobook intake and tagging app for Windows + Docker Desktop. FastAPI, SQLite, a separate background worker, FFmpeg/ffprobe, and a browser review UI. No OpenAI key required.

## Start safely

```powershell
git clone https://github.com/rickycatto2/AudiobookFunnel.git
cd AudiobookFunnel
docker compose up -d --build
```

Open **http://localhost:8095**. Without `.env`, Docker uses isolated `data/source`, `data/torrents`, `data/work`, and `data/library` folders beside the project. Monitoring, automatic approval, torrent submission, and Audiobookshelf integration start **disabled**. Put a completed sample book in `data/source`, then click **Check completed downloads**.

To connect your actual folders, copy `.env.example` to `.env`, check the paths, and recreate containers:

```powershell
Copy-Item .env.example .env
# Edit .env to match your machine before the next command.
docker compose up -d --force-recreate
```

| Windows host default in example | Container path / Settings |
| --- | --- |
| `D:/Downloads/audiobooks/raw` | `/source` (read-only) |
| `D:/Downloads/torrent-inbox` | `/torrents` (read-only) |
| `D:/Downloads/audiobooks/funnel-work` | `/work` |
| `D:/Audiobooks` | `/library` |
| Project `data/config` | `/config` (SQLite + settings) |

Settings can select subdirectories within these mounts. Host folders are controlled by Docker, so changing a Windows host path requires `.env` and container recreation. Do not point source at incomplete downloads. qBittorrent must finish and move downloads into `raw` before discovery. No source files are renamed, modified, or deleted. Work and final library paths must never overlap source paths.

## Review a book

1. Check completed downloads, or enable monitoring in Settings. A source directory is a package, potentially containing multiple books. Each root audio file is also accepted as a package.
2. Open a job. Inspect duration, filenames, warnings, and ordering. Multi-file jobs always require grouping confirmation. **Review package grouping** lets you assign files to numbered books, set their order, and explicitly exclude samples. It resets metadata for that package; do this before metadata editing.
3. Search Audible by title/author or paste an ASIN or Audible URL. Select a candidate to populate metadata; inspect its score breakdown. If unavailable, try a different Audible region in Settings, Google Books, Open Library, existing tags, or manual editing.
4. Edit the fields and choose embedded artwork, a local image, a provider cover, or no cover. Field provenance is shown below each input. Save edits before changing search/selection. Preview your final naming.
5. **Save & approve** queues the book. **Process now** overrides the heavy-processing window for that job. Queued jobs can return to review before the worker claims them.
6. The worker copies and checks sources, creates an M4B, writes tags/art, verifies duration and full audio decode, generates sidecars, then publishes the complete folder. Errors retain source and work files. Fix the problem, save, and approve again.

AAC sources with compatible stream parameters are copied without re-encoding. Other sources convert once to AAC. Multi-part source chapters are offset and preserved; unchaptered files become part chapters. Final M4B metadata includes title, author, narrator, year, series/position, genre, description, publisher, identifiers and language. Artwork uses its original JPEG/PNG bytes. `reader.html` is a new simple readable info page, not an exact copy of your previous MP3Tag template.

Default naming matches the prior workflow:

```text
Author/Series/Year - Title [Series 1]/Title (Year) [Series 1] - Author.m4b
Author/Year - Title/Title (Year) - Author.m4b
```

Use the Settings templates to change it. Optional `year_prefix`, `year_suffix`, and `series_suffix` omit punctuation when values are missing. Blank series directories disappear. Windows-invalid characters are sanitized. Existing destinations are never intentionally overwritten: duplicates go to ERROR.

## Integrations

**qBittorrent:** Set its URL (often `http://host.docker.internal:8080`), username/password, and the save path **as qBittorrent sees it**, typically `/downloads/audiobooks/raw`. Enable inbox submission after checking the mount mapping. `.torrent` files are uploaded via the Web API; no watch-folder dependency. SQLite tracks submitted file hashes; originals remain in the inbox. qBittorrent's existing Gluetun/VPN configuration is untouched. This version does not manage torrent deletion or seeding settings. Drop complete `.torrent` files atomically; failed submissions are retried.

**Audiobookshelf:** Set URL, API token, and library ID, then enable. Successful finalized batches queue a scan request. Transient failures retry every 60 seconds without reprocessing audio. `ACCEPTED` means the server accepted the scan request; indexing completion is not monitored. Credentials are stored in local SQLite and redacted in Settings responses. Empty secret inputs preserve the existing value. Disable an integration to stop its use.

**Audible reference:** The metadata behavior follows [the user's MP3Tag Audible-via-API source](https://github.com/binyaminyblatt/mp3tag-Audible-via-API): public catalog search/direct ASIN lookup and separate author, narrator, series, description and artwork fields. No upstream code is copied. The unofficial Audible API may restrict editions or change. Google Books and Open Library supply print-book metadata and never auto-approve an audiobook edition.

Google Books can enforce an anonymous quota. If it reports a rate limit, use Open Library or add an optional Google Books API key in Settings. The key is stored and redacted like the other integration secrets.

## Confidence and scheduling

Fixed evidence weights: title 35, author 25, narrator 10, runtime 15, series 5, series number 5, ASIN/ISBN 5. Missing evidence scores zero; identifier conflict blocks automation. Auto-approval also requires confirmed grouping, strong title/author agreement, runtime evidence, and the configured lead over the next result. Scores are evidence measures, not statistical probabilities. Default threshold 85, lead 12, automation off.

Heavy encodes start 01:00–07:00 America/Chicago by default. Equal start/end means all day. Running work finishes after the window closes. AAC remux/tagging can run anytime. One worker processes one book at a time with a configurable FFmpeg thread limit.

## Operations and recovery

```powershell
docker compose logs --tail 100 worker
docker compose ps
docker compose stop
docker compose up -d
```

Back up `data/config` with containers stopped, along with `.env` and your library. SQLite is the state authority. The worker holds an exclusive OS lock; do not scale it horizontally. On restart, interrupted processing returns to READY. A matching final manifest and SHA-256 checksum recover publication that finished just before a crash. Encodes restart from retained source copies; they do not resume halfway through audio. `funnel.json` retains metadata, provenance, source-relative names and output checksum.

Publication paths are frozen when processing starts, so changing naming settings during recovery does not create a second copy. An error with a saved publication plan offers **Retry unchanged publication**. Editing a failed job clears that plan only when its own final output has not already been published.

No automatic cleanup is enabled. Private job work folders and abandoned hidden `.funnel-*` library staging folders may remain after failures; remove them manually only after verifying successful outputs and stopping the worker. Source changes detected after inspection require a new import under a new source-package name in this initial release. Existing package paths are deliberately not rediscovered as new books.

The web service binds to localhost and has same-origin JSON mutation checks. It has no user accounts or authentication: do not expose port 8095 publicly. Configure Audiobookshelf to ignore hidden staging folders and prefer the explicit post-finalization scan over filesystem watching if your deployment notices partial imports.

## Development and validation

Python 3.12+ and FFmpeg/ffprobe on PATH:

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.lock
.\.venv\Scripts\python -m pip install -e .
.\.venv\Scripts\python -m pytest -q
```

Tests use temporary mounts and generated audio; they never need your audiobook files or provider credentials. They cover real remux/transcode/tag/cover/sidecar publication, grouping/order/exclusions, deduplication, immutable sources, collisions, settings/secret handling, deterministic scoring, ASIN parsing, and independent scan retries. Integration credentials and real services still need deployment-specific validation. API docs: `/docs`.

See [architecture notes](docs/architecture.md). Initial limitations: no archive extraction, no cancellation of an active encode, no automatic cleanup, no cross-package merge, no exact previous reader.html template, no whole-database reconstruction from manifests, and no live tracking of Audiobookshelf scan completion.
