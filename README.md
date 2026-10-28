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
| RECONCILE_SECONDS | `3600`; idle fallback check, `0` disables |
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
When there is no actionable work it waits up to RECONCILE_SECONDS (one hour by
default), or until a webhook arrives. Each check compares the current Kapowarr
mappings and skips unchanged files. This catches manual imports/fixes once
Kapowarr has mapped them. It does not force Kapowarr to rescan disk or force a
full Kavita library scan. New files still get a settling follow-up before writing.
Health heartbeats continue during the wait.

Webhook verification is stored against a hash of the configured token and survives
restart. A changed/missing token returns to five-minute fallback polling until
verified again. Set RECONCILE_SECONDS to 0 to restore webhook-only idle behavior.
The hourly interval starts after the latest completed check; webhook checks reset
it because they perform the same comparison.


## Discord notifications

Optionally set `DISCORD_WEBHOOK_URL` in the tagger's Compose environment to a
Discord channel webhook URL (or use `DISCORD_WEBHOOK_URL_FILE`). Keep it private;
anyone with this URL can post to that channel. Recreate the container after changing
the environment. No Discord configuration is needed in Kapowarr or Kavita. Keep
Kapowarr's existing download-completed webhook to the tagger enabled.

Notices cover written metadata, CBR conversions, new review/unmapped items, cycle
failures and recovery, and Kavita connection/scan request problems. The first check
after enabling notifications includes existing review/unmapped items as a baseline.
Preview can report attention/errors but never announces proposed changes as applied.
Repeated identical failures and unchanged checks stay quiet. Deduplication is stored
in `/state/discord-state.json` and survives restarts. Mentions are disabled.

Scan accepted means Kavita accepted the request, not that import is finished.
Discord delivery is best-effort: failures are logged without credentials and never
stop tagging. Persistent attention/error notices retry on the next processing cycle;
transient completion messages are not queued for later delivery. A stopped container
cannot notify you; this is not a replacement for external uptime monitoring.


## Optional 30-day cleanup and history restoration

Cleanup is **off by default** and is independent of normal tagging. No current
installation deletes files merely by pulling this update. Create a Kavita reading
list named Keep, add books you want retained, and use the numeric ID from its URL:

```yaml
CLEANUP_MODE: "preview"
KEEP_READING_LIST_ID: "12" # Replace with your actual /lists/NUMBER
CLEANUP_DAYS: "30"
CLEANUP_INTERVAL_SECONDS: "3600"
CLEANUP_MAX_DELETIONS: "10"
```

Preview persists observations and writes `/state/retention-report.json`; it does
not unmonitor/delete files or restore progress. The report includes candidate paths,
first-observed completion times and counts. Unread, Keep-protected, absent,
changed, or ambiguous books lose their countdown. The date means **first observed
complete**, not an inferred historical finish date. Newly observed old reads get
30 days too. Review preview before setting `CLEANUP_MODE: "write"`; write also
requires the existing tagger `MODE: write`. Days cannot be less than 30. The
countdown observed in preview carries over into write mode. Cleanup runs on the
existing service loop, with a maximum idle interval of CLEANUP_INTERVAL_SECONDS;
long processing cycles can delay it.

### What gets protected

The Keep list is mandatory: missing, inaccessible or malformed responses block
cleanup. An intentionally empty accessible list protects no books. Membership is
checked twice again immediately before a deletion. A reading list protects its
current chapters, not future books in a series. A whole TPB/omnibus must be 100%
read; we never remove individual pages. Only one-to-one Kavita chapter/file and
Kapowarr issue/file mappings with confirmed CV volume and issue IDs are eligible.
Files awaiting tagging/conversion, review, and multi-file/multi-issue or duplicate
mappings are skipped. Keep your reader's progress synchronized to Kavita.

### Permanent history log

`/state/history/historical-log.json` stores schema/account scope, countdowns,
book metadata and IDs, SHA-256 hashes, original progress, available series reading
session history, restoration records, and deletion outcomes. It is atomically
replaced, synced to disk and read back before deletion. History export/API/disk
failures block deletion. No comic archive copies are retained. Back up this small
state directory separately. The log contains private reading history.

Immediately before deleting, the service exports history, unmonitors the mapped
Kapowarr issue, verifies it is unmonitored, rechecks Keep/progress/file identity,
and deletes via Kapowarr's file API. A cancelled/failed operation may leave the
issue unmonitored; the log records cancellation rather than silently re-enabling
future downloads. Ten deletions per check is the default limit. After deletion,
a full scan of the affected Kavita library is requested, with a persistent retry
queue. Accepted still means queued, not finished. A crash/timeout during deletion
can leave `delete_requested`, meaning the outcome needs review rather than being
reported as a confirmed deletion.

### Reimport restoration

An exact match of both CV volume+issue identity and file SHA-256 can restore
completed status automatically in cleanup write mode. Kavita IDs are not used as
cross-import identity. Existing partial reread progress is preserved. Same CV IDs
with different archive bytes are protected for manual review, never guessed;
retagging/repacking may change an archive hash. A reimport skips deletion that
cycle and starts a fresh 30-day observation countdown afterward if complete.
Preview reports restoration candidates without changing Kavita.

Kavita 0.9.1.4 supports `Reader/mark-chapter-read` with
`generateReadingSession: false`. Restoration therefore sets completion without
inventing replacement reading sessions. Original session timestamps/durations,
ratings, bookmarks, annotations and reading-list membership are **not replayed**
into Kavita; the exported history remains in JSON. Kavita may trigger its usual
external scrobbling when completion is restored. This is not a full account backup.

The history scope includes a hash of the Kavita URL and API key. Changing either
blocks reuse of the log pending review, to avoid restoring another account's
progress. Unsupported API shapes fail closed. The implementation is based on
Kavita 0.9.1.4 and the silasfelinus Kapowarr API; automated tests use isolated files
and mocked APIs, not destructive live-library tests.

Discord sends one notice when a countdown starts (30 days by default), another
when three days or fewer remain, and a confirmation after deletion. Notices include
the scheduled date; preview warnings explicitly say deletion is disabled. Completion
restoration is also reported. Warning milestones and an outbox persist across
restarts. Failed deliveries retry on later cleanup checks; cancelled countdown
warnings are discarded. Each book is sent separately (at most 20 per check), so
large batches are not silently truncated. Reset countdowns get new warnings;
switching preview to write issues an appropriately labeled warning. Delivery is
best-effort and does not delay deletion; downtime or Discord failures can prevent
advance notice. An overdue countdown is not sent a misleading three-day warning.
 Inspect the JSON log for authoritative
outcomes, especially after an interrupted cycle. `CLEANUP_MODE: off` disables
cleanup and restoration without affecting tagging or deleting the history log.
