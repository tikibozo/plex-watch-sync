from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import create_app

from .conftest import FakePool


def _episode_payload(
    *,
    event: str = "stop",
    username: str = "alice",
    rating_key: str = "12345",
    grandparent_rating_key: str = "9000",
    view_offset: str = "60000",
) -> dict:
    return {
        "event": event,
        "username": username,
        "rating_key": rating_key,
        "grandparent_rating_key": grandparent_rating_key,
        "view_offset": view_offset,
        "media_type": "episode",
    }


def test_watched_event_mirrors_to_other_users(client: TestClient, pool: FakePool) -> None:
    r = client.post("/webhook", json=_episode_payload(event="watched"))
    assert r.status_code == 200
    assert r.json() == {"action": "mirrored", "event": "watched", "targets": ["bob"]}
    assert pool.watched_calls == [("bob", 12345)]
    assert pool.offset_calls == []


def test_stop_event_above_threshold_mirrors_offset(client: TestClient, pool: FakePool) -> None:
    r = client.post("/webhook", json=_episode_payload(view_offset="60000"))
    assert r.status_code == 200
    assert r.json()["action"] == "mirrored"
    assert pool.offset_calls == [("bob", 12345, 60000)]


def test_stop_event_below_threshold_is_ignored(client: TestClient, pool: FakePool) -> None:
    # min_offset_ms in the test config is 30000.
    r = client.post("/webhook", json=_episode_payload(view_offset="5000"))
    assert r.status_code == 200
    body = r.json()
    assert body["action"] == "ignored"
    assert "below min" in body["reason"]
    assert pool.offset_calls == []


def test_unknown_user_is_ignored(client: TestClient, pool: FakePool) -> None:
    r = client.post("/webhook", json=_episode_payload(username="carol"))
    assert r.status_code == 200
    assert r.json()["action"] == "ignored"
    assert pool.watched_calls == [] and pool.offset_calls == []


def test_non_episode_media_is_ignored(client: TestClient, pool: FakePool) -> None:
    payload = _episode_payload(event="watched")
    payload["media_type"] = "movie"
    r = client.post("/webhook", json=payload)
    assert r.status_code == 200
    assert r.json()["action"] == "ignored"
    assert pool.watched_calls == []


def test_unlabeled_show_is_ignored(client: TestClient, pool: FakePool) -> None:
    r = client.post(
        "/webhook",
        json=_episode_payload(event="watched", grandparent_rating_key="7777"),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["action"] == "ignored"
    assert body["reason"] == "show not labeled"
    assert pool.watched_calls == []


def test_missing_rating_key_returns_400(client: TestClient) -> None:
    payload = _episode_payload()
    payload["rating_key"] = ""
    r = client.post("/webhook", json=payload)
    assert r.status_code == 400


def test_invalid_rating_key_returns_400(client: TestClient) -> None:
    payload = _episode_payload()
    payload["rating_key"] = "not-a-number"
    r = client.post("/webhook", json=payload)
    assert r.status_code == 400


def test_invalid_json_returns_400(client: TestClient) -> None:
    r = client.post(
        "/webhook",
        content="this is not json",
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 400
    assert r.json()["detail"] == "invalid JSON"


def test_non_object_json_returns_400(client: TestClient) -> None:
    r = client.post("/webhook", json=[1, 2, 3])
    assert r.status_code == 400
    assert "object" in r.json()["detail"]


def test_all_targets_failed_returns_502(config_path: str) -> None:
    pool = FakePool(labeled_keys={9000}, fail_users={"bob"})
    app = create_app(config_path=config_path, pool=pool)
    with TestClient(app) as c:
        r = c.post("/webhook", json=_episode_payload(event="watched"))
    assert r.status_code == 502
    detail = r.json()["detail"]
    assert detail["failed_targets"] == ["bob"]


def test_partial_failure_returns_partial(tmp_path) -> None:
    # Three-user config so we can have one success + one failure.
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        """\
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
  - name: carol
    token_env: PLEX_TOKEN_CAROL
"""
    )
    pool = FakePool(labeled_keys={9000}, fail_users={"bob"})
    app = create_app(config_path=str(cfg), pool=pool)
    with TestClient(app) as c:
        r = c.post("/webhook", json=_episode_payload(event="watched"))
    assert r.status_code == 200
    body = r.json()
    assert body["action"] == "partial"
    assert body["targets"] == ["carol"]
    assert body["failed_targets"] == ["bob"]


def test_webhook_returns_503_before_first_refresh(config_path: str) -> None:
    pool = FakePool(labeled_keys=set(), raise_on_refresh=True)
    app = create_app(config_path=config_path, pool=pool)
    with TestClient(app) as c:
        # Lifespan ran, refresh raised, ready stays False.
        h = c.get("/healthz").json()
        assert h["ready"] is False
        r = c.post("/webhook", json=_episode_payload(event="watched"))
    assert r.status_code == 503
    assert pool.watched_calls == []


def test_healthz_reports_ready_and_set_size(client: TestClient) -> None:
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["ready"] is True
    assert body["status"] == "ok"
    assert body["shared_set_size"] == 1
    assert body["users"] == ["alice", "bob"]
