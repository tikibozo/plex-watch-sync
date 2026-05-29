import logging
from typing import Protocol

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
    def __init__(self, config: Config) -> None:
        self._config = config
        self._by_user: dict[str, PlexServer] = {}

    def get(self, user: User) -> PlexServer:
        client = self._by_user.get(user.name)
        if client is None:
            client = PlexServer(self._config.plex_url, user.token())
            self._by_user[user.name] = client
        return client

    def _fetch(self, user: User, rating_key: int):
        plex = self.get(user)
        item = plex.fetchItem(rating_key)
        if item is None:
            raise RuntimeError(
                f"plex returned no item for ratingKey={rating_key} (user={user.name})"
            )
        return item

    def list_labeled_show_keys(self) -> set[int]:
        # Plex labels are server-wide metadata; any token can read them.
        # Use the first configured user's client.
        reader = self._config.users[0]
        plex = self.get(reader)
        keys: set[int] = set()
        for library_name in self._config.libraries:
            library = plex.library.section(library_name)
            for show in library.search(label=self._config.label_name):
                keys.add(int(show.ratingKey))
        return keys

    def mark_watched(self, user: User, rating_key: int) -> None:
        self._fetch(user, rating_key).markPlayed()

    def set_offset(self, user: User, rating_key: int, time_ms: int) -> None:
        self._fetch(user, rating_key).updateTimeline(time_ms, state="stopped")
