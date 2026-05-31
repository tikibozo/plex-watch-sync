# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); the project
follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.5.1] — 2026-05-31

### Fixed

- `mark_watched` (and the in-show reconciler) now clears any lingering
  `viewOffset` before setting the watched flag, by toggling
  unplayed → played when the offset is non-zero. Without this, Plex's
  `/:/scrobble` endpoint left the offset in place, so mirrored episodes
  stayed in the target user's "Continue Watching" carousel forever
  even though `viewCount` was 1.

## [0.5.0] — 2026-05-31

### Fixed

- Trailing **Playback Stop** events no longer downgrade a freshly-mirrored
  watched mark. Tautulli often fires **Watched** at ~90% and then
  **Playback Stop** for the same play with `view_offset` near the end;
  the trailing `/:/progress` call was overwriting the watched flag with
  "in-progress at 99%". The service now pre-checks each target's
  `viewCount` and skips the offset update when the target already has
  the episode marked watched.

### Changed

- `mark_watched` and `set_offset` now operate via per-user
  `PlexServer` instances (cached), matching `reconcile_show`. Removed
  the dual code path that used the owner's HTTP session with the
  target's token in the query string.

### Added

- `PoolProtocol.is_watched(user, rating_key)` for the pre-check.

## [0.4.0] — 2026-05-30

### Added

- Auto-reconcile on newly-labeled shows. When a show appears in the
  labeled set for the first time, the service catches every configured
  user up to the union of watched-state across accounts: for each
  episode, if any user has it watched, mark it watched for everyone;
  otherwise, raise the lagging users' `view_offset` to the maximum
  across users. Never un-watches and never rewinds.
- State file at `/app/state/seen_keys.json` so container restarts
  don't replay reconciliation. First run (no state file) initializes
  with current labeled shows recorded as already-seen, *without*
  reconciling — so a fresh deployment doesn't surprise-mutate any
  long-standing labels. Unlabel + relabel is the manual "redo
  reconcile" trigger.
- `STATE_PATH` environment variable.

## [0.3.0] — 2026-05-30

### Fixed

- Non-owner Plex accounts now mirror correctly. Previously the service
  constructed `PlexServer(url, user_token)` for every configured user;
  that 401s for shared friends (their plex.tv token is rejected at the
  server root). The service now identifies the owner among configured
  users and exchanges each friend's plex.tv token for a server-specific
  access token via `MyPlexAccount.resources()`, matched on
  `machineIdentifier`.

## [0.2.1] — 2026-05-29

### Added

- Each dropped webhook event is now logged with a reason — "user not
  in sync set", "show not labeled", "offset below min", etc. Replaces
  the silent `200 OK` responses that made diagnosis painful.

## [0.2.0] — 2026-05-29

### Removed

- `TAUTULLI_WEBHOOK_SHARED_SECRET` / `X-Sync-Secret` support. Tautulli's
  stock webhook agent (verified through v2.17.1) does not expose a
  custom-headers field in the UI, so the header could never actually
  be sent. Trust the internal-only Docker network for isolation.

## [0.1.0] — 2026-05-29

### Added

- Initial release. Bidirectional Plex watched/progress mirror between
  configured accounts on one server, scoped by a Plex label.
- FastAPI service with readiness gating (`/webhook` returns 503 until
  the first Plex label refresh succeeds) and partial-failure handling
  (502 if all targets fail, 200 `action=partial` if some).
- Multi-stage Dockerfile (`python:3.13-slim`, non-root uid 10001).
- Release workflow on tag push: builds and pushes
  `ghcr.io/<owner>/plex-watch-sync:<semver>` to GHCR.
