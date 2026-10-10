# Kapowarr Tagger

Headless companion for silasfelinus/Kapowarr and Kavita. Kapowarr identifies and
organizes comics; this service makes their embedded grouping metadata consistent
and requests a Kavita scan after a quiet batch. No web UI or exposed ports.

**Default mode is preview.** Write mode replaces CBZ archives after verification.
This is early software: the Deadpool test grouped eight issues correctly in the
owner's Kavita installation. Full-library validation and NAS container deployment
are still pending. The service integration is covered by automated tests; live
Kavita scan notification has not yet been exercised.

## What it does

- Polls Kapowarr's `/api/volumes` and individual volume endpoints every five minutes.
- Uses confirmed issue-to-file mappings; it does not infer identity from filenames.
- Requires stable file size/mtime across observations and a five-minute settling period.
- Stores file fingerprints and desired identity signatures in persistent state.
- Writes only `Series`, `Volume`, and `Number`. Existing summaries, credits, page
  metadata, and other fields are retained. Missing descriptive metadata is not scraped.
- Uses `Series = title [CV-volume-ID]`, `Volume = run starting year`, and Kapowarr's
  issue number. The visible CV suffix intentionally separates same-name/same-year
  series and trades. CBL names may need remapping to this identity.
- Streams one temporary replacement archive at a time, verifies the SHA-256 of
  every non-metadata member, and detects source changes before replacement.
- Keeps only original ComicInfo bytes and archive comments in small JSON rollback
  records. **It does not retain duplicate comic archives.** These records support
  metadata rollback, not byte-for-byte restoration of ZIP compression or recovery
  from disk failure. A temporary archive still needs space roughly equal to one book.
- Writes a durable scan-request flag before changes. Calls
  `POST /api/Library/scan?libraryId=1&force=false` using `x-api-key` after the batch.
  Failed requests retry next cycle without rewriting completed books. HTTP acceptance
  means queued, not that Kavita finished scanning or grouped everything correctly.
- Reports unmapped files, unsupported formats, invalid archives, multi-issue mappings,
  missing identity, and grouping overrides without changing those files.

## Container setup

Use `compose.stack.yaml` as a template for a Kapowarr stack plus the
companion, or merge `compose.addition.yaml` into an existing stack. The companion
must be on the same Docker network as Kapowarr. It uses UID 1029 and GID 100.

Create these directories on the NAS, writable/readable as appropriate by UID 1029:

```text
/path/to/config/kapowarr-tagger/state
/path/to/config/kapowarr-tagger/secrets
```

Set `KAPOWARR_API_KEY`, `KAVITA_API_KEY`, and `WEBHOOK_TOKEN` directly in the
Compose `environment:` section. The supplied templates contain empty values to
fill in privately; never commit populated keys. No secret-file mounts are needed.
The webhook token must be random, URL-safe, and at least 24 characters.

Optional file-based configuration remains available using `KAPOWARR_API_KEY_FILE`,
`KAVITA_API_KEY_FILE`, and `WEBHOOK_TOKEN_FILE`. A configured `_FILE` takes precedence;
an unreadable or empty file does not silently fall back to the environment value.
The Kavita key must belong to an account permitted to scan libraries.
The service refuses credential-bearing redirects.

Start in `MODE: preview` while the existing bulk writer runs. Do not run two writers
against the same files. The service lock only protects instances sharing its state
folder; it cannot lock Kapowarr or a separate local writer.

```sh
docker compose up -d kapowarr-tagger
docker compose logs -f kapowarr-tagger
```

Inspect `/state/report.json`. The first cycle observes files; later cycles inspect
stable files. After the bulk run and grouping verification, stop the local writer,
disable Kavita's independent folder watcher and scheduled scans, and set `MODE` to
`write` in Compose. Redeploy the companion. Do not disable Kavita scanning until
its credentials and the companion's scan request have been verified.

Files still settling delay their folder scan. Files held for review remain in
the library and can still be seen by Kavita when it scans; this service does not
quarantine them. Continuous incoming files may postpone a scan until the library
is quiet. Kapowarr and other tools must not replace an archive while it is being
tagged; the writer detects changes but a shared filesystem offers no universal lock.

## Configuration

| Variable | Default / example |
|---|---|
| MODE | `preview` or explicitly `write` |
| KAPOWARR_URL | `http://kapowarr:5656` (append any configured URL base) |
| KAPOWARR_API_KEY_FILE | `/run/secrets/kapowarr_api_key` |
| KAVITA_URL | `http://kavita:5000` |
| KAVITA_API_KEY_FILE | `/run/secrets/kavita_api_key` |
| KAVITA_COMICS_ROOT | path inside Kavita, e.g. `/comics` |
| COMICS_ROOT | `/comics`; must match paths returned by Kapowarr |
| STATE_DIR | `/state`; must be outside the comics root |
| POLL_SECONDS | `300`, minimum 60 |
| SETTLE_SECONDS | `300` |
| VOLUME_IDS | Optional comma-separated Kapowarr internal IDs, not CV IDs |
| RUN_ONCE | `true` for one cycle; settling state persists between invocations |

State contains a journal, current report, incremental cache, scan retry flag,
health timestamp, and metadata rollback records. Keep it persistent between updates.
Do not publish it: it contains your local library filenames and metadata.
A stale health timestamp flags failed polling; consult report.json separately for
pending scan requests and held files. Large initial batches can exceed the health
window without being stuck. Stop signals finish the current book before exiting.

## Publishing and tests

GitHub Actions runs all tests and builds linux/amd64 and linux/arm64 images at
`ghcr.io/thef4tdaddy/kapowarr-tagger:preview` and a commit-specific tag. Tagged
releases get their own image tags. No Docker Hub credentials are required.
The GitHub package must be public for anonymous pulls; repository visibility alone
may not set this. Pin a tested commit/version tag for production.

```sh
python3 -m unittest discover -v
```

`companion.py` remains a standalone read-only planner. `writer.py` applies an explicit
reviewed plan with source hashes; `apply_library.py` resumes a local bulk plan.
Neither CLI is automatically invoked alongside the container service.

## Kapowarr webhook and folder scans

The companion now requests `/api/Library/scan-folder` for each changed folder,
with a durable, deduplicated retry queue. It no longer calls the library scan
endpoint. Kavita may internally broaden a folder request to a library scan,
especially when discovering a new series; HTTP acceptance is not scan completion.

Set `KAVITA_COMICS_ROOT` to the comic library mount **inside Kavita**. The companion
translates paths relative to `COMICS_ROOT`; these roots need not be identical.
A wrong mapping can result in Kavita ignoring the request.

Set a random URL-safe `WEBHOOK_TOKEN` (at least 24 characters) in Compose,
then configure a generic webhook in Kapowarr:
`http://kapowarr-tagger:8080/hooks/YOUR_TOKEN`, subscribed to `download_completed`.
Keep the token private. No host port needs publishing when both containers share
the Compose network. The listener rejects missing/incorrect tokens and oversized
requests; it does not log the token-bearing URL.

The fork's payload has no reliable file ID, so webhooks wake an incremental
comparison against Kapowarr mappings, not a direct single-file operation. Existing
unchanged files are skipped. Stable-file checks still apply, and five-minute
polling catches missed notifications/manual imports. A folder held for review or
still settling is not submitted until those conditions clear. The listener is
optional when neither WEBHOOK_TOKEN nor WEBHOOK_TOKEN_FILE is set.


## CBR support

Set `CONVERT_CBR: "true"` to enable CBR-to-CBZ conversion in write mode. Preview
mode reports `conversion_proposed` and leaves the source unchanged. Image bytes
are preserved; there is no resizing or lossy recompression. The converter streams
RAR entries through libarchive, verifies content hashes, adds the grouping tags,
and verifies the published CBZ before deleting its source CBR. Existing CBZ targets
are never overwritten; collisions require review. Encrypted, damaged, unsafe, or
unsupported archives remain unchanged.

Conversion needs temporary disk space for the new archive and verification; it
does not retain a permanent full-file backup. Small original metadata backups are
kept, but these cannot reconstruct the original RAR container byte for byte.
Kapowarr receives a `refresh_and_scan` task for the affected volume after conversion,
with a durable retry queue. Its next API snapshot should reflect the new extension.
Kavita receives the folder notification. Keep other writers and independent scans
from touching the same book during conversion. A crash may leave both formats;
that collision is held for review rather than automatically deleting either file.


## Preview connection check

Every cycle, including preview, makes an authenticated GET to Kavita's
`/api/Library/libraries`. Logs and report.json (`kavita_connection`) report
connection/authentication status without exposing keys or response bodies.
Failures do not stop the metadata preview. This does not request a scan,
change reading history, or prove the account has scan permissions. A live folder
notification remains the final write-mode integration check.

Authenticated download-completed webhooks log
`Kapowarr webhook received: download_completed; incremental check requested`.
Other authenticated events log that they were ignored. Tokens, URLs and payload
contents are never included in these messages. Receipt requests a check; it does
not imply tagging or scanning has finished.


## Adaptive scheduling

On startup the service performs a catch-up check. Until it receives an authenticated
`download_completed` webhook (including Kapowarr's matching test event), it polls
every POLL_SECONDS (300 by default). It then switches to event-driven checks, with
SETTLE_SECONDS follow-ups for settling files and POLL_SECONDS retries for failures.
When there is no actionable work it waits for a webhook, without scanning the
library or calling either API. Review-only files do not cause endless polling.
Health heartbeats continue while idle, so Docker does not mark a quiet service unhealthy.

Webhook verification is stored against a hash of the configured token and survives
restart. A changed/missing token returns to fallback polling until verified again.
After switching, missed webhooks or direct filesystem additions are caught by the
next webhook or startup catch-up, not periodic reconciliation. Changing the token
requires updating the Kapowarr notification too.
