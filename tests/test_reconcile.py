from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app

from .conftest import FakePool


def _start_app(config_path: str, state_path: str, pool: FakePool) -> TestClient:
    app = create_app(config_path=config_path, pool=pool, state_path=state_path)
    return TestClient(app)


def test_first_run_initializes_without_reconcile(
    config_path: str, state_path: str
) -> None:
    """When the state file does not exist, the very first refresh records
    every currently-labeled show as 'seen' WITHOUT triggering reconcile —
    we don't want a fresh deployment to suddenly cross-pollinate accounts
    for shows the user labeled long ago."""
    pool = FakePool(labeled_keys={9000, 9001})
    with _start_app(config_path, state_path, pool):
        pass
    assert pool.reconcile_calls == []
    saved = json.loads(Path(state_path).read_text())
    assert set(saved["seen"]) == {9000, 9001}


def test_new_label_triggers_reconcile(
    config_path: str, state_path: str
) -> None:
    """If the state file exists and a show appears in the labeled set that
    isn't there yet, reconcile fires and the show is added to seen."""
    Path(state_path).write_text(json.dumps({"seen": [9000]}))

    pool = FakePool(labeled_keys={9000, 9001})
    with _start_app(config_path, state_path, pool):
        pass

    assert pool.reconcile_calls == [9001]
    saved = json.loads(Path(state_path).read_text())
    assert set(saved["seen"]) == {9000, 9001}


def test_restart_with_state_file_does_not_reconcile_again(
    config_path: str, state_path: str
) -> None:
    """Container restart with the state file intact must not re-reconcile
    shows we've already seen."""
    Path(state_path).write_text(json.dumps({"seen": [9000, 9001]}))

    pool = FakePool(labeled_keys={9000, 9001})
    with _start_app(config_path, state_path, pool):
        pass

    assert pool.reconcile_calls == []


def test_removing_a_label_drops_it_from_seen_without_reconcile(
    config_path: str, state_path: str
) -> None:
    """Unlabeling a show drops it from seen so a later re-label re-triggers
    reconcile, but the unlabel itself doesn't do anything destructive."""
    Path(state_path).write_text(json.dumps({"seen": [9000, 9001]}))

    pool = FakePool(labeled_keys={9000})  # 9001 unlabeled
    with _start_app(config_path, state_path, pool):
        pass

    assert pool.reconcile_calls == []
    saved = json.loads(Path(state_path).read_text())
    assert set(saved["seen"]) == {9000}


def test_relabeling_after_unlabel_retriggers_reconcile(
    config_path: str, state_path: str
) -> None:
    """End-to-end: unlabel + relabel is a usable manual reconcile trigger."""
    Path(state_path).write_text(json.dumps({"seen": [9000]}))

    # First refresh: unlabel
    pool = FakePool(labeled_keys=set())
    with _start_app(config_path, state_path, pool):
        pass
    assert pool.reconcile_calls == []
    assert set(json.loads(Path(state_path).read_text())["seen"]) == set()

    # Second refresh: relabel — should reconcile.
    pool2 = FakePool(labeled_keys={9000})
    with _start_app(config_path, state_path, pool2):
        pass
    assert pool2.reconcile_calls == [9000]


def test_reconcile_failure_does_not_mark_seen(
    config_path: str, state_path: str
) -> None:
    """If reconcile raises, the show stays out of seen so the next refresh
    retries."""
    Path(state_path).write_text(json.dumps({"seen": []}))

    class FailingPool(FakePool):
        def reconcile_show(self, show_rating_key: int) -> dict:
            self.reconcile_calls.append(show_rating_key)
            raise RuntimeError("boom")

    pool = FailingPool(labeled_keys={9000})
    with _start_app(config_path, state_path, pool):
        pass

    assert pool.reconcile_calls == [9000]
    saved = json.loads(Path(state_path).read_text())
    assert saved["seen"] == []
