import logging
from typing import Protocol

from plexapi.exceptions import Unauthorized
from plexapi.myplex import MyPlexAccount
from plexapi.server import PlexServer

from .config import Config, User

logger = logging.getLogger(__name__)


class PoolProtocol(Protocol):
    """Structural interface the FastAPI app depends on.

    ClientPool is the production implementation; tests inject a fake.
    """

    def list_labeled_show_keys(self) -> set[int]: ...

    def is_watched(self, user: User, rating_key: int) -> bool: ...

    def mark_watched(self, user: User, rating_key: int) -> None: ...

    def set_offset(self, user: User, rating_key: int, time_ms: int) -> None: ...

    def reconcile_show(self, show_rating_key: int) -> dict: ...


class ClientPool:
    """Talks to Plex on behalf of N configured users.

    Plex tokens fall into two classes:
      - The server owner's plex.tv token doubles as the server access token,
        so PlexServer(url, owner_token) works directly.
      - A friend's plex.tv token is rejected at the server's root endpoint
        (401). To act as that user on this server, we have to exchange their
        plex.tv token for a server-specific access token via
        MyPlexAccount.resources(), matched against the server's
        machineIdentifier.

    On first use the pool discovers which configured user can act as owner
    (i.e. whose token PlexServer accepts directly). Library reads and the
    label refresh use the owner. For every other user, we resolve and
    cache a server access token, then call /:/scrobble and /:/progress
    over plain HTTP with that token in the query string.
    """

    def __init__(self, config: Config) -> None:
        self._config = config
        self._owner_server: PlexServer | None = None
        self._owner_user: User | None = None
        # Per-user *server* access tokens (not the plex.tv account tokens).
        self._server_token_by_user: dict[str, str] = {}
        # Per-user PlexServer wrapping that user's server access token.
        self._friend_servers: dict[str, PlexServer] = {}

    def _get_owner_server(self) -> PlexServer:
        if self._owner_server is not None:
            return self._owner_server
        last_err: Exception | None = None
        for user in self._config.users:
            try:
                server = PlexServer(self._config.plex_url, user.token())
            except Unauthorized as exc:
                last_err = exc
                logger.info(
                    "user %s cannot act as server owner (401); will use as friend",
                    user.name,
                )
                continue
            self._owner_server = server
            self._owner_user = user
            self._server_token_by_user[user.name] = user.token()
            logger.info("using %s as server-owner token for library reads", user.name)
            return server
        raise RuntimeError(
            "no configured user could connect to Plex as owner"
        ) from last_err

    def _server_token(self, user: User) -> str:
        cached = self._server_token_by_user.get(user.name)
        if cached:
            return cached
        owner = self._get_owner_server()
        machine_id = owner.machineIdentifier
        account = MyPlexAccount(token=user.token())
        for resource in account.resources():
            if resource.clientIdentifier == machine_id:
                token = resource.accessToken
                if not token:
                    continue
                self._server_token_by_user[user.name] = token
                logger.info("resolved server access token for %s", user.name)
                return token
        raise RuntimeError(
            f"user {user.name} has no shared access to this Plex server "
            f"(machineIdentifier={machine_id})"
        )

    def _friend_server(self, user: User) -> PlexServer:
        cached = self._friend_servers.get(user.name)
        if cached is not None:
            return cached
        token = self._server_token(user)
        server = PlexServer(self._config.plex_url, token)
        self._friend_servers[user.name] = server
        return server

    def _fetch_item(self, user: User, rating_key: int):
        server = self._friend_server(user)
        item = server.fetchItem(rating_key)
        if item is None:
            raise RuntimeError(
                f"plex returned no item for ratingKey={rating_key} (user={user.name})"
            )
        return item

    def list_labeled_show_keys(self) -> set[int]:
        plex = self._get_owner_server()
        keys: set[int] = set()
        for library_name in self._config.libraries:
            library = plex.library.section(library_name)
            for show in library.search(label=self._config.label_name):
                keys.add(int(show.ratingKey))
        return keys

    def is_watched(self, user: User, rating_key: int) -> bool:
        item = self._fetch_item(user, rating_key)
        return (getattr(item, "viewCount", 0) or 0) > 0

    def mark_watched(self, user: User, rating_key: int) -> None:
        self._fetch_item(user, rating_key).markPlayed()

    def set_offset(self, user: User, rating_key: int, time_ms: int) -> None:
        self._fetch_item(user, rating_key).updateTimeline(time_ms, state="stopped")

    def reconcile_show(self, show_rating_key: int) -> dict:
        """Bring all configured users to the union of watched-state for one show.

        For every episode of the show:
        - If ANY user has watched it → mark watched for everyone who hasn't.
        - Otherwise, if any user has a view_offset > config.min_offset_ms beyond
          the max we'd already record → set everyone's offset to the max.

        Never un-watches and never rewinds. Returns a summary dict.
        """
        per_user: dict[str, dict[int, object]] = {}
        for user in self._config.users:
            try:
                token = self._server_token(user)
                server = PlexServer(self._config.plex_url, token)
                show = server.fetchItem(show_rating_key)
                if show is None:
                    logger.warning(
                        "reconcile: user %s has no access to show ratingKey=%d",
                        user.name,
                        show_rating_key,
                    )
                    continue
                per_user[user.name] = {ep.ratingKey: ep for ep in show.episodes()}
            except Exception:
                logger.exception(
                    "reconcile: failed to fetch show %d for user %s",
                    show_rating_key,
                    user.name,
                )

        if len(per_user) < 2:
            logger.info(
                "reconcile show=%d: fewer than 2 users could load; skipping",
                show_rating_key,
            )
            return {"show": show_rating_key, "skipped": True, "users": list(per_user)}

        all_ep_keys: set[int] = set()
        for eps in per_user.values():
            all_ep_keys.update(eps.keys())

        marked = 0
        offset_updated = 0
        errors = 0

        for ep_key in sorted(all_ep_keys):
            any_watched = False
            max_offset = 0
            for eps in per_user.values():
                ep = eps.get(ep_key)
                if ep is None:
                    continue
                if (getattr(ep, "viewCount", 0) or 0) > 0:
                    any_watched = True
                offset = getattr(ep, "viewOffset", 0) or 0
                if offset > max_offset:
                    max_offset = offset

            for name, eps in per_user.items():
                ep = eps.get(ep_key)
                if ep is None:
                    continue
                current_watched = (getattr(ep, "viewCount", 0) or 0) > 0
                current_offset = getattr(ep, "viewOffset", 0) or 0
                if any_watched and not current_watched:
                    try:
                        ep.markPlayed()
                        marked += 1
                    except Exception:
                        errors += 1
                        logger.exception(
                            "reconcile: markPlayed failed for user=%s ep=%d",
                            name,
                            ep_key,
                        )
                elif (
                    not any_watched
                    and max_offset > current_offset + self._config.min_offset_ms
                ):
                    try:
                        ep.updateTimeline(max_offset, state="stopped")
                        offset_updated += 1
                    except Exception:
                        errors += 1
                        logger.exception(
                            "reconcile: updateTimeline failed for user=%s ep=%d",
                            name,
                            ep_key,
                        )

        summary = {
            "show": show_rating_key,
            "users": list(per_user),
            "episodes_considered": len(all_ep_keys),
            "watched_marked": marked,
            "offset_updated": offset_updated,
            "errors": errors,
        }
        logger.info("reconcile done: %s", summary)
        return summary
