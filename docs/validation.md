# Initial release validation — 2026-09-24

- 15 tests pass on Windows (Python 3.14) with the host FFmpeg/ffprobe.
- The same 15 tests pass inside the Linux Docker image (Python 3.13).
- Docker Compose builds and starts both services; web healthcheck is healthy.
- Docker mount inspection confirms `/source` and `/torrents` are read-only.
- Browser smoke test: discover a synthetic AAC file → inspect warning → confirm grouping → edit description → preview final naming → approve → COMPLETE. Output includes M4B, description, reader page, and manifest. The completed record survives container recreation.
- Live Audible title/author search and direct ASIN extracted from an Audible URL return metadata, descriptions and artwork.
- Live Open Library fallback returns Cory Doctorow titles.
- Live anonymous Google Books request returned HTTP 429. Mapping/API-key behavior is tested with a deterministic response; the Settings UI accepts an optional key. Live Google Books success remains dependent on provider quota.
- qBittorrent submission/deduplication and Audiobookshelf scan failure/retry are tested with simulated HTTP responses. No real torrent was submitted and no real Audiobookshelf server was scanned; credentials must be configured and verified for the user's deployment.

Media tests exercise AAC stream copy, MP3 conversion, mixed parts, chapter generation, embedded artwork byte preservation, final tags/sidecars, source checksums, destination collision refusal, and recovery without changing the frozen destination after settings change. Other tests exercise grouping/order/exclusions, processing-window override and job claiming, secrets redaction, path validation, same-origin mutations, confidence signals and ASIN parsing.

The demo deployment uses only project-local `data/` folders. Production Windows library/download folders are not connected. Monitoring and integrations remain disabled. The synthetic demo book is clearly labeled in the queue.

Non-blocking test warning: the installed Starlette test client warns about future migration from httpx to httpx2. Application requests and all tests pass with the locked dependencies.
