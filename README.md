# plex-watch-sync

Ever watch a show with someone and have to go look to see where you left off on the other person's account? This syncs watch progress for specific shows between the configured accounts so you can pick up where you left off from either account. This works with standard Plex accounts and a single server, and currently leverages Tautulli for watch/stopped events.

## Goal

Mirror Plex watched state and view-offset between multiple Plex accounts on the same server, scoped by a Plex label.

When two (or more) people watch a show together but each starts sessions on their own Plex account, the watched flag and resume position live on whichever account played the episode. The other account has to find the right episode and seek. `plex-watch-sync` listens for playback events and mirrors the state to the other configured accounts — but only for shows you've opted in to.

The opt-in mechanism is a Plex label (default: `sync`). Any show without the label is ignored, so solo viewing of unrelated content does not pollute the other account's progress.

## How it works

```
+-----------+   Playback Stop / Watched    +-----------------+
| Tautulli  | ----------------------------> | plex-watch-sync |
| (per-user |   webhook (JSON)              | (FastAPI)       |
|  notifier)|                               +--------+--------+
+-----------+                                        |
                                          /:/scrobble or
                                          /:/progress
                                          (with each user's token)
                                                     v
                                                  +------+
                                                  | Plex |
                                                  +------+
```

- Tautulli (or any equivalent Plex-event source) POSTs a small JSON payload to `plex-watch-sync` on **Watched** and **Playback Stop**.
- The service refreshes the set of labeled shows from Plex every 10 minutes.
- If the event's show is in that set and the user is in the configured user list, the service calls Plex's `/:/scrobble` (watched) or `/:/progress` (partial offset) endpoint using each *other* user's token.
- Those endpoints mutate watched state without creating a play session, so Tautulli does not re-fire — no feedback loop.

When Tautulli fires **Watched** and **Playback Stop** in close sequence for the same play (the common case of "watched to 99% and stopped"), the service pre-checks each target's current watched flag before issuing the trailing `/:/progress` call and **skips targets that already have it marked watched**. This avoids downgrading a freshly-mirrored watched mark back to "in-progress at 99%".

## Quick start (Docker Compose)

```yaml
services:
  plex-watch-sync:
    image: ghcr.io/tikibozo/plex-watch-sync:latest
    container_name: plex-watch-sync
    restart: unless-stopped
    environment:
      PLEX_TOKEN_ALICE: ${PLEX_TOKEN_ALICE}
      PLEX_TOKEN_BOB: ${PLEX_TOKEN_BOB}
    volumes:
      - ./config.yaml:/app/config.yaml:ro
      - ./state:/app/state           # writable; holds reconcile bookkeeping
    networks:
      - plex
    # No host port published; reached via Docker DNS at
    # http://plex-watch-sync:38491/
```

`config.yaml`:

```yaml
plex_url: http://plex:32400
label_name: sync
refresh_interval_seconds: 600
min_offset_ms: 30000
libraries:
  - "TV Shows"
users:
  - name: alice
    token_env: PLEX_TOKEN_ALICE
  - name: bob
    token_env: PLEX_TOKEN_BOB
```

`name` must match the Plex *username* (login handle, case-sensitive) — not the display/friendly name. Look it up in Tautulli's Users page or in Plex.tv account settings.

Each `token_env` names an environment variable holding that user's Plex token.
See [Getting a Plex token](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/).

## Configuring Tautulli

For **each** user you want to sync, add a Notification Agent (Settings → Notification Agents → Add → Webhook):

- **Webhook URL**: `http://plex-watch-sync:38491/webhook`
- **Webhook Method**: POST
- **Triggers**: Watched, Playback Stop
- **Conditions**: `Username` `is` `<that one user's exact Plex username>`
- **Data → Watched → JSON Data**:
  ```json
  {"event":"watched","username":"{username}","rating_key":"{rating_key}","grandparent_rating_key":"{grandparent_rating_key}","view_offset":"{view_offset}","media_type":"{media_type}"}
  ```
- **Data → Playback Stop → JSON Data**: same payload, but `"event":"stop"`.

The per-user `Username is …` condition is what scopes each agent to one account. Without it, every user on the server would generate events.

## Auto-reconcile on label

When the service notices a show *newly appearing* in the labeled set (i.e. the show wasn't in the previous refresh and is now), it runs a one-time reconciliation across every configured user for that show:

- For each episode of the show, if **any** configured user has it marked watched, mark it watched for every user who hasn't.
- Otherwise, if any user has a `view_offset` that exceeds another user's by more than `min_offset_ms`, set the larger offset on the lagging users.

The reconciler **never un-watches** an episode and **never rewinds** an offset — it only catches lagging accounts up to the most-progressed account. This handles the common case of "we were watching this show separately, now we want to share progress."

The set of already-reconciled shows is persisted to a state file (default `/app/state/seen_keys.json`) so container restarts don't re-run reconcile, and so unlabeling + re-labeling acts as a manual "redo reconcile" trigger.

**First run is a special case:** when the state file doesn't yet exist, the first refresh after startup records every currently-labeled show as already-seen *without* reconciling. Otherwise upgrading the service for the first time would silently cross-pollinate every show you'd ever labeled.

The corollary: if you label a show **before** the service is running, no reconcile will fire. Either label while the service is up, or unlabel + relabel after it starts.

## Marking a show for sync

In Plex, edit the show, add label `sync` (or whatever you set `label_name` to) under Tags → Labels, save. The service picks it up on the next refresh 
(default ≤10 minutes) or on container restart. Remove the label to stop mirroring future activity for that show.

## Webhook contract

`POST /webhook` accepts:

```json
{
  "event": "stop",
  "username": "alice",
  "rating_key": "12345",
  "grandparent_rating_key": "6789",
  "view_offset": "62000",
  "media_type": "episode"
}
```

`event` is either `"watched"` (episode crossed Plex's ~90% threshold — service calls `/:/scrobble`) or `"stop"` (playback stopped mid-episode — service calls `/:/progress` with `view_offset`). `media_type` must be `"episode"`; movies and other types are ignored.

All values are strings (Tautulli sends them that way; the service parses ints internally). Numeric fields may be empty strings; the service treats empty as 0.

There is no authentication on `/webhook` — the service is intended to run on an internal-only Docker network where only trusted senders can reach it.

Responses:

- `200` with `{"action": "mirrored", "event": "...", "targets": ["bob"]}` when every other configured user got the update.
- `200` with `{"action": "partial", "event": "...", "targets": ["bob"], "failed_targets": ["carol"]}` when at least one target succeeded and at least one failed.
- `200` with `{"action": "ignored", "reason": "..."}` if the event is dropped on purpose (unknown user, unlabeled show, sub-threshold offset, non-episode media).
- `400` on malformed payloads (invalid JSON, non-object body, missing/invalid `rating_key`).
- `502` with `{"action": "failed", "event": "...", "failed_targets": [...]}` when every target user's Plex update failed. Sender (Tautulli) sees the failure instead of a false 200.
- `503` until the first label-set refresh from Plex succeeds. The background loop keeps retrying; check `/healthz` `ready` field.

## Health

`GET /healthz` returns:

```json
{
  "status": "ok",
  "ready": true,
  "last_refresh": 1735689600.0,
  "shared_set_size": 3,
  "users": ["alice", "bob"]
}
```

`ready` is `true` once the first label-set refresh from Plex has succeeded. Until then, `status` is `"starting"`, `last_refresh` is `0.0`, and `/webhook` returns `503`. The container's own healthcheck still passes (the endpoint returns 200 either way), so a slow or initially-unreachable Plex doesn't flap the container — but downstream senders see `503` until the service is actually able to mirror.

`shared_set_size` is the count of show ratingKeys carrying the configured label across all configured libraries.

## Configuration reference

| Key | Default | Description |
| --- | --- | --- |
| `plex_url` | (required) | HTTP URL of the Plex Media Server (e.g. `http://plex:32400`). |
| `label_name` | `sync` | The Plex label that opts a show in. |
| `refresh_interval_seconds` | `600` | How often to re-read labeled shows from Plex. |
| `min_offset_ms` | `30000` | Stop events below this view-offset are dropped (avoids mirroring false starts). |
| `libraries` | (required) | List of Plex library names (as shown in the Plex UI sidebar) to scan for labeled shows. |
| `users` | (required) | List of `{name, token_env}` records. `name` is the case-sensitive Plex username; `token_env` names the env var holding that user's token. |

Environment variables:

| Var | Description |
| --- | --- |
| `PLEX_TOKEN_*` | Per-user Plex tokens, named to match each user's `token_env` in config. |
| `CONFIG_PATH` | Optional. Path to `config.yaml`. Defaults to `/app/config.yaml`. |
| `STATE_PATH` | Optional. Path to the seen-keys JSON state file. Defaults to `/app/state/seen_keys.json`. The containing directory must be writable by the container's uid (10001). |
| `LOG_LEVEL` | Optional. Standard Python log levels. Defaults to `INFO`. |

## Limitations & known constraints

These come from things we hit running this against a real Plex deployment.

- **TV episodes only.** Movies are out of scope. The same code path would work for movies, but the label-gating story is different (you'd label individual movies rather than a parent), so it's not implemented.
- **Tautulli is the reference event source.** Any other source that POSTs the documented JSON payload to `/webhook` will work, but Tautulli is what's been tested.
- **No real-time playback following.** This is a watched-state mirror, not a "stay in sync while we both watch on different clients" tool.
- **No webhook authentication.** Tautulli's stock webhook agent (verified through v2.17.1) has no custom-headers field in the UI, so a shared-secret header can't be sent. The service is intended to run on an internal-only Docker network where only trusted senders can reach it.
- **Owner + friends model.** Configure the Plex server owner plus N "friend" accounts (separate plex.tv accounts that have been shared this server's libraries). Plex Home/managed users are not tested — they may need different handling because Plex stores their state under the parent plex.tv account.
- **Outbound to `plex.tv` required at startup.** For each non-owner configured user, the service makes one `MyPlexAccount.resources()` call to exchange their plex.tv token for a server-specific access token. After the first event involving a given friend, that token is cached for the process lifetime.
- **`mark-watched` clears `viewOffset` explicitly.** Plex's `/:/scrobble` does not zero `viewOffset` — leaving it non-zero keeps the episode in the "Continue Watching" carousel even when `viewCount=1`. The service works around this by toggling unplayed → played when the offset is non-zero. If you build a similar tool from scratch, don't get caught by this.
- **Sub-30-second stops are ignored.** `min_offset_ms` defaults to 30 s; below that we drop the event to avoid mirroring false starts (seek attempts, accidental plays).
- **State file is self-rebuilding.** `/app/state/seen_keys.json` only records which labeled shows have already been auto-reconciled. Deleting it triggers a fresh first-run init (no reconcile of pre-existing labels). It is not a backup-critical file.

## Development

```bash
uv sync --all-extras
uv run uvicorn app.main:app --reload --port 38491
uv run pytest
uv run ruff check .
```

`config.yaml` for local dev should point `plex_url` at a Plex you can reach, and the `PLEX_TOKEN_*` env vars must be set. The test suite uses an injected fake pool, so it runs without a real Plex.

### Releasing

Releases are managed by [release-please](https://github.com/googleapis/release-please). Commits to `main` must use [Conventional Commits](https://www.conventionalcommits.org/) prefixes (`feat:`, `fix:`, `chore:`, `docs:`, `ci:`, `refactor:`). release-please opens a release PR that bumps `pyproject.toml`, regenerates `CHANGELOG.md`, and tags `vX.Y.Z` when merged. The tag triggers the multi-arch image publish to GHCR. No manual version edit or changelog write-up is required.

## License

MIT — see [LICENSE](LICENSE).
