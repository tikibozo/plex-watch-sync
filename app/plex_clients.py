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

    def mark_watched(self, user: User, rating_key: int) -> None: ...

    def set_offset(self, user: User, rating_key: int, time_ms: int) -> None: ...


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

    def list_labeled_show_keys(self) -> set[int]:
        plex = self._get_owner_server()
        keys: set[int] = set()
        for library_name in self._config.libraries:
            library = plex.library.section(library_name)
            for show in library.search(label=self._config.label_name):
                keys.add(int(show.ratingKey))
        return keys

    def mark_watched(self, user: User, rating_key: int) -> None:
        owner = self._get_owner_server()
        token = self._server_token(user)
        owner._session.get(
            f"{self._config.plex_url}/:/scrobble",
            params={
                "identifier": "com.plexapp.plugins.library",
                "key": str(rating_key),
                "X-Plex-Token": token,
            },
            timeout=10,
        ).raise_for_status()

    def set_offset(self, user: User, rating_key: int, time_ms: int) -> None:
        owner = self._get_owner_server()
        token = self._server_token(user)
        owner._session.get(
            f"{self._config.plex_url}/:/progress",
            params={
                "identifier": "com.plexapp.plugins.library",
                "key": str(rating_key),
                "time": str(time_ms),
                "state": "stopped",
                "X-Plex-Token": token,
            },
            timeout=10,
        ).raise_for_status()
