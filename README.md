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

Settings can select subdirectories within these mounts. Host folders are controlled by Docker, so changing a Windows host path requires `.env` and container recreation. Do not point source at incomplete downloads. qBittorrent must finish and move downloads into `raw` before discovery. Funnel never writes to source files directly. Optional completed-download archiving asks qBittorrent to move originals only after seeding and processing are finished. Work and final library paths must never overlap source paths.

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

Use the Settings templates to change it. Optional `year_prefix`, `year_suffix`, and `series_suffix` omit punctuation when values are missing. Blank series directories disappear. Windows-invalid characters are sanitized. Existing destinations are never intentionally overwritten: collisions go to Already exists.

## Integrations

**qBittorrent:** Set its URL (often `http://host.docker.internal:8080`), username/password, and the save path **as qBittorrent sees it**, typically `/downloads/audiobooks/raw`. Enable inbox submission after checking the mount mapping. `.torrent` files are uploaded via the Web API; no watch-folder dependency. SQLite tracks submitted file hashes; originals remain in the inbox. qBittorrent's existing Gluetun/VPN configuration is untouched. This version does not manage torrent deletion or seeding settings. Drop complete `.torrent` files atomically; failed submissions are retried.

**Audiobookshelf:** Set URL, API token, and library ID, then enable. Successful finalized batches queue a scan request. Transient failures retry every 60 seconds without reprocessing audio. `ACCEPTED` means the server accepted the scan request; indexing completion is not monitored. Credentials are stored in local SQLite and redacted in Settings responses. Empty secret inputs preserve the existing value. Disable an integration to stop its use.

**Audible reference:** The metadata behavior follows [the user's MP3Tag Audible-via-API source](https://github.com/binyaminyblatt/mp3tag-Audible-via-API): public catalog search/direct ASIN lookup and separate author, narrator, series, description and artwork fields. No upstream code is copied. The unofficial Audible API may restrict editions or change. Google Books and Open Library supply print-book metadata and never auto-approve an audiobook edition.

Google Books can enforce an anonymous quota. If it reports a rate limit, use Open Library or add an optional Google Books API key in Settings. The key is stored and redacted like the other integration secrets.

## Confidence and scheduling

Base evidence weights: title 35, author 25, narrator 10, runtime 15, series 5, series number 5, ASIN/ISBN 5. Missing optional evidence earns no base points, but an Audible candidate with exact normalized title and author and runtime within both 1% and 120 seconds receives a **95-point score floor** when no edition evidence conflicts. The breakdown shows the base evidence and strong-match adjustment explicitly. This lets standalone books pass without series, narrator or identifier tags. Scores are rule-based evidence measures, not statistical probabilities.

For single-file books, clear `Title - Author` or `Author - Title` filenames provide independent title/author evidence. The full author segment and title must match, not just substrings. A series-label album (such as `Speakeasy Series`), blank title, or generic numbered track title can be replaced for matching purposes. The original tags remain visible and unchanged until you select/approve metadata. Conflicting substantive titles/authors still block automatic approval. Multi-file groups do not borrow their identity from a single track name. New discovery also uses an unambiguous filename title for searching when the embedded title is generic and the author is known. Existing review jobs are rescored without reimporting.

Conflicting identifiers, narrator, series/position or language block automation. Confirmed grouping, enabled auto-approval, the configured threshold, and the configured lead over a competing edition still apply. Duplicate results for the same Audible ASIN do not count as competing editions. Default threshold 85, lead 12; defaults leave automation off. Your saved settings are retained across upgrades.

Existing REVIEW jobs display freshly calculated scores when opened. **Auto-match & queue** applies the current rules to stored candidates and queues an eligible job, preserving manual field edits and following the processing window. Save grouping/edits first. If blocked, the panel explains why. No existing review jobs are silently approved during an upgrade.

## Clear stale errors

If an older script moved a source away, open the failed job and click **Dismiss error**. Failed package inspections also have this button. The error disappears from the active queue but retains its original error and audit history; no source, working, or library files are deleted. Choose **Dismissed errors** in the queue filter and click **Restore error** to bring it back. Restoring does not start processing. Discovery remembers dismissed packages so they are not repeatedly imported.

Heavy encodes start 01:00–07:00 America/Chicago by default. Equal start/end means all day. Running work finishes after the window closes. AAC remux/tagging can run anytime. One worker processes one book at a time with a configurable FFmpeg thread limit.

## Operations and recovery

Books whose final destination already exists now stop with **Already exists**, separately from processing errors. Existing collision errors are reclassified on startup. This means the folder is occupied; it does not prove the two recordings are identical, and it does not find duplicates stored under different names. Both copies are retained. Use the **Already exists** queue filter, then **Review metadata / naming** if the book is a different edition you want to keep. Save corrected metadata or naming before approving again. No library scan is requested for a skipped collision.

```powershell
docker compose logs --tail 100 worker
docker compose ps
docker compose stop
docker compose up -d
```

Back up `data/config` with containers stopped, along with `.env` and your library. SQLite is the state authority. The worker holds an exclusive OS lock; do not scale it horizontally. On restart, interrupted processing returns to READY. A matching final manifest and SHA-256 checksum recover publication that finished just before a crash. Encodes restart from retained source copies; they do not resume halfway through audio. `funnel.json` retains metadata, provenance, source-relative names and output checksum.

Publication paths are frozen when processing starts, so changing naming settings during recovery does not create a second copy. An error with a saved publication plan offers **Retry unchanged publication**. Editing a failed job clears that plan only when its own final output has not already been published.

Download archiving is disabled by default (see below). No automatic deletion is enabled. Private job work folders and abandoned hidden `.funnel-*` library staging folders may remain after failures; remove them manually only after verifying successful outputs and stopping the worker. Source changes detected after inspection require a new import under a new source-package name in this initial release. Existing package paths are deliberately not rediscovered as new books.

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

See [architecture notes](docs/architecture.md). Initial limitations: no archive extraction, no cancellation of an active encode, no automatic working-folder cleanup, no cross-package merge, no exact previous reader.html template, no whole-database reconstruction from manifests, and no live tracking of Audiobookshelf scan completion.

## Archive completed downloads after seeding

1. Create a processed-download folder outside RAW and your library, for example `D:/Downloads/audiobooks/processed`. Add `ARCHIVE_PATH=D:/Downloads/audiobooks/processed` to `.env`, then run `docker compose up -d --build`. It is mounted read-only at `/archive`; qBittorrent performs all moves and deletions.
2. Save the qBittorrent Web UI address and login in Settings. **Test saved qBittorrent connection** must succeed. Torrent inbox submission can remain off.
3. Under **Finished download archive**, set the RAW and processed paths as qBittorrent sees them (for native Windows qBittorrent: `D:/Downloads/audiobooks/raw` and `D:/Downloads/audiobooks/processed`). Leave the Docker processed path at `/archive`. Enable archiving.
4. Keep qBittorrent's seeding-limit action at **Stop torrent**. Funnel reads the current per-torrent or global total-seeding-time limit each poll, including changes from 24 hours to longer limits. If no total-time limit is enabled, it can use a ratio limit. Inactive-time-only limits are deliberately not used as proof that seeding is finished. A total-time limit takes precedence when both time and ratio limits are configured. Turn off Automatic Torrent Management for archive candidates; Funnel does not change categories or seeding settings.
5. Open **Download archive** to see waiting, blocked, moving, or **Ready to clear** downloads. Each torrent moves into a separate folder named by its torrent hash. The worker checks about once a minute when it is free; an active audio encode can delay the check.
6. Use **Clear archived download** for a specific verified archive. Confirm the displayed torrent name. The worker rechecks the stopped state, seeding limit, archive contents, shared files, and library checksums, then asks qBittorrent to delete that archive and its torrent entry. No automatic deletion runs merely because a download was archived.

Every audio file in a torrent must map to known, finished jobs. A package containing multiple books waits for all its books. Excluded/unrecognized audio, stale source files, unresolved reviews, errors, shared seeding files, or ambiguous mappings block cleanup. A library naming collision alone never permits cleanup: open **Already exists**, check the existing recording, and choose **Keep library copy; allow download cleanup**. The confirmed library audio is checksummed before its source is eligible. This confirmation can return to review until an archive move is planned.

Move and deletion intents persist across restarts. qBittorrent's reported location and actual mounted files must agree before a download becomes Ready to clear. Archive contents are hashed before and after moving, and library audio is verified again before clearing. Unknown or manually relocated torrents are left for attention. Use the clear button instead of deleting archive folders in Explorer so qBittorrent stays consistent. A missing API connection stops cleanup; it does not affect normal book processing. Processing work folders remain separate and are not cleared by this feature.
