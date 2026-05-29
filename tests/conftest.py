from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import User
from app.main import create_app

CONFIG_TEMPLATE = """\
plex_url: http://plex.invalid:32400
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
"""


class FakePool:
    """In-memory stand-in for ClientPool.

    Records calls to mark_watched / set_offset so tests can assert
    fan-out behavior, and lets each test control:
      - which show ratingKeys are 'labeled' (visible in the shared set)
      - which users raise on mirror calls (to exercise partial / total failure)
      - whether the initial label refresh raises (to exercise readiness gating)
    """

    def __init__(
        self,
        labeled_keys: set[int] | None = None,
        fail_users: set[str] | None = None,
        raise_on_refresh: bool = False,
    ) -> None:
        self.labeled_keys = set(labeled_keys or set())
        self.fail_users = set(fail_users or set())
        self.raise_on_refresh = raise_on_refresh
        self.watched_calls: list[tuple[str, int]] = []
        self.offset_calls: list[tuple[str, int, int]] = []

    def list_labeled_show_keys(self) -> set[int]:
        if self.raise_on_refresh:
            raise RuntimeError("plex unreachable (test)")
        return set(self.labeled_keys)

    def mark_watched(self, user: User, rating_key: int) -> None:
        if user.name in self.fail_users:
            raise RuntimeError(f"mock failure for {user.name}")
        self.watched_calls.append((user.name, rating_key))

    def set_offset(self, user: User, rating_key: int, time_ms: int) -> None:
        if user.name in self.fail_users:
            raise RuntimeError(f"mock failure for {user.name}")
        self.offset_calls.append((user.name, rating_key, time_ms))


@pytest.fixture
def config_path(tmp_path: Path) -> str:
    p = tmp_path / "config.yaml"
    p.write_text(CONFIG_TEMPLATE)
    return str(p)


@pytest.fixture
def pool() -> FakePool:
    # By default, ratingKey 9000 represents an opted-in show.
    return FakePool(labeled_keys={9000})


@pytest.fixture
def client(
    config_path: str, pool: FakePool, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    monkeypatch.delenv("TAUTULLI_WEBHOOK_SHARED_SECRET", raising=False)
    app = create_app(config_path=config_path, pool=pool)
    with TestClient(app) as c:
        yield c
