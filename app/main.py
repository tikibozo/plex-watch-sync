import asyncio
import contextlib
import json
import logging
import os
import time

from fastapi import FastAPI, HTTPException, Request

from .config import Config, load_config
from .plex_clients import ClientPool, PoolProtocol

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("plex_watch_sync")


DEFAULT_STATE_PATH = "/app/state/seen_keys.json"


class State:
    def __init__(
        self,
        config: Config,
        pool: PoolProtocol,
        state_path: str | None = None,
    ) -> None:
        self.config = config
        self.pool = pool
        self.shared_keys: set[int] = set()
        self.last_refresh: float = 0.0
        self.ready: bool = False
        self.state_path: str = state_path or os.environ.get(
            "STATE_PATH", DEFAULT_STATE_PATH
        )
        # seen_keys: shows we've already observed-and-reconciled. Persisted so
        # restarts don't trigger spurious reconcile, and so the very first run
        # (file missing) is distinguishable from "no labels yet".
        self.seen_keys: set[int] = set()
        self._state_loaded: bool = False
        self._load_state()
        self._refresh_task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    def _load_state(self) -> None:
        try:
            with open(self.state_path) as f:
                data = json.load(f)
            self.seen_keys = {int(k) for k in data.get("seen", [])}
            self._state_loaded = True
            logger.info("loaded %d seen keys from %s", len(self.seen_keys), self.state_path)
        except FileNotFoundError:
            self.seen_keys = set()
            self._state_loaded = False
            logger.info("no state file at %s — first run", self.state_path)
        except Exception:
            self.seen_keys = set()
            self._state_loaded = False
            logger.exception("failed to load state from %s; starting fresh", self.state_path)

    def _save_state(self) -> None:
        try:
            d = os.path.dirname(self.state_path)
            if d:
                os.makedirs(d, exist_ok=True)
            tmp = self.state_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"seen": sorted(self.seen_keys)}, f)
            os.replace(tmp, self.state_path)
        except Exception:
            logger.exception("failed to save state to %s", self.state_path)

    async def refresh_shared_keys(self) -> None:
        keys = await asyncio.to_thread(self.pool.list_labeled_show_keys)
        added = keys - self.seen_keys
        removed = self.seen_keys - keys

        self.shared_keys = keys
        self.last_refresh = time.time()
        self.ready = True
        logger.info(
            "refreshed shared set: %d shows across %s",
            len(keys),
            self.config.libraries,
        )

        # Drop unlabel-ed shows from seen so re-labeling re-triggers reconcile.
        if removed:
            self.seen_keys -= removed

        # On very first run (no state file ever existed), don't reconcile any
        # already-labeled shows. Users opt in to reconciliation by labeling a
        # show while the service is running. After this first refresh, seen
        # is bootstrapped to match the current shared set.
        if added and not self._state_loaded and not self.seen_keys:
            logger.info(
                "first run: initializing seen with %d existing labeled show(s); "
                "no reconcile",
                len(added),
            )
            self.seen_keys = set(keys)
            self._state_loaded = True
            self._save_state()
            return

        if removed and not added:
            self._save_state()
            return

        for key in sorted(added):
            logger.info("reconciling newly-labeled show ratingKey=%d", key)
            try:
                await asyncio.to_thread(self.pool.reconcile_show, key)
            except Exception:
                logger.exception("reconcile failed for ratingKey=%d", key)
                # Don't mark seen — retry on the next refresh.
                continue
            self.seen_keys.add(key)
            self._save_state()

    async def refresh_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=self.config.refresh_interval_seconds,
                )
                # _stop was set — exit cleanly.
                return
            except TimeoutError:
                pass
            try:
                await self.refresh_shared_keys()
            except Exception:
                logger.exception("shared-set refresh failed")

    async def start(self) -> None:
        # Best-effort initial refresh. If Plex is unreachable at boot,
        # don't crash — the background loop will retry, and /webhook
        # returns 503 until self.ready flips true.
        try:
            await self.refresh_shared_keys()
        except Exception:
            logger.exception(
                "initial refresh failed; webhook will return 503 until a "
                "background refresh succeeds"
            )
        self._refresh_task = asyncio.create_task(self.refresh_loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._refresh_task is not None:
            await self._refresh_task


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    state: State = app.state.sync
    await state.start()
    try:
        yield
    finally:
        await state.stop()


def _parse_int(value: str | int | None) -> int:
    if value is None or value == "":
        return 0
    return int(value)


def _ignore(reason: str, **fields: object) -> dict:
    extras = " ".join(f"{k}={v}" for k, v in fields.items() if v is not None)
    logger.info("ignored: %s%s", reason, " (" + extras + ")" if extras else "")
    return {"action": "ignored", "reason": reason}


async def handle_event(state: State, payload: dict) -> dict:
    event = payload.get("event")
    username = payload.get("username")
    media_type = payload.get("media_type")

    if media_type != "episode":
        return _ignore("media_type not episode", user=username, media_type=media_type)
    if event not in {"watched", "stop"}:
        return _ignore("unknown event", user=username, event=event)

    user = next((u for u in state.config.users if u.name == username), None)
    if user is None:
        return _ignore("user not in sync set", user=username)

    try:
        grandparent_key = _parse_int(payload.get("grandparent_rating_key"))
        rating_key = _parse_int(payload.get("rating_key"))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"invalid rating key: {exc}") from exc
    if rating_key == 0:
        raise HTTPException(status_code=400, detail="missing rating_key")
    if grandparent_key not in state.shared_keys:
        return _ignore(
            "show not labeled",
            user=username,
            event=event,
            grandparentRatingKey=grandparent_key,
            ratingKey=rating_key,
        )

    view_offset_ms: int | None = None
    if event == "stop":
        try:
            view_offset_ms = _parse_int(payload.get("view_offset"))
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=f"invalid view_offset: {exc}") from exc
        if view_offset_ms < state.config.min_offset_ms:
            return _ignore(
                f"offset {view_offset_ms}ms below min",
                user=username,
                ratingKey=rating_key,
            )

    targets = [u for u in state.config.users if u.name != username]
    mirrored: list[str] = []
    failed: list[str] = []
    for target in targets:
        try:
            if event == "watched":
                await asyncio.to_thread(state.pool.mark_watched, target, rating_key)
            else:
                offset = view_offset_ms or 0
                await asyncio.to_thread(
                    state.pool.set_offset, target, rating_key, offset
                )
        except Exception:
            logger.exception(
                "mirror failed: event=%s ratingKey=%s target=%s",
                event,
                rating_key,
                target.name,
            )
            failed.append(target.name)
            continue
        mirrored.append(target.name)

    offset_part = f" offset={view_offset_ms}ms" if view_offset_ms is not None else ""
    logger.info(
        "event=%s user=%s ratingKey=%d%s mirrored=%s failed=%s",
        event,
        username,
        rating_key,
        offset_part,
        mirrored,
        failed,
    )

    if failed and not mirrored:
        # All targets failed — surface as 502 so Tautulli's notifier logs
        # the failure instead of treating it as a success.
        raise HTTPException(
            status_code=502,
            detail={
                "action": "failed",
                "event": event,
                "failed_targets": failed,
            },
        )
    if failed:
        return {
            "action": "partial",
            "event": event,
            "targets": mirrored,
            "failed_targets": failed,
        }
    return {"action": "mirrored", "event": event, "targets": mirrored}


def create_app(
    config_path: str | None = None,
    *,
    pool: PoolProtocol | None = None,
    state_path: str | None = None,
) -> FastAPI:
    path = config_path or os.environ.get("CONFIG_PATH", "/app/config.yaml")
    config = load_config(path)
    if pool is None:
        pool = ClientPool(config)
    state = State(config, pool, state_path=state_path)

    app = FastAPI(lifespan=lifespan, title="plex-watch-sync")
    app.state.sync = state

    @app.get("/healthz")
    async def healthz() -> dict:
        return {
            "status": "ok" if state.ready else "starting",
            "ready": state.ready,
            "last_refresh": state.last_refresh,
            "shared_set_size": len(state.shared_keys),
            "users": [u.name for u in config.users],
        }

    @app.post("/webhook")
    async def webhook(request: Request) -> dict:
        if not state.ready:
            raise HTTPException(
                status_code=503,
                detail="shared set not yet loaded — try again shortly",
            )
        try:
            payload = await request.json()
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail="invalid JSON") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="payload must be a JSON object")
        return await handle_event(state, payload)

    return app


def _create_default_app() -> FastAPI | None:
    """Build the production app, returning None if no config is available.

    Allows ``import app.main`` for tooling (linters, test discovery) on a
    machine without /app/config.yaml.
    """
    try:
        return create_app()
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        logger.warning("app not constructed at import time: %s", exc)
        return None


app = _create_default_app()
