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
| `LOG_LEVEL` | Optional. Standard Python log levels. Defaults to `INFO`. |

## Limitations

- Only TV episodes are mirrored. Movies are out of scope (same code path would work but the label-gating story is different and not yet implemented).
- Tautulli is the reference event source. Any other source that POSTs the same JSON payload to `/webhook` will work.
- This is a watched-state mirror, not a real-time playback follower. There is no attempt to keep both clients in sync while playback is active.

## Development

```bash
uv sync --all-extras
uv run uvicorn app.main:app --reload --port 38491
uv run pytest
uv run ruff check .
```

`config.yaml` for local dev should point `plex_url` at a Plex you can reach, and the `PLEX_TOKEN_*` env vars must be set. The test suite uses an injected fake pool, so it runs without a real Plex.

## License

MIT — see [LICENSE](LICENSE).
