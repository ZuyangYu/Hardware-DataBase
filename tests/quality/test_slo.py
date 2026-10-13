from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.observability.slo import ChatSloPolicy, ChatSloStore
from tests.quality.test_feedback import quality as quality_fixture  # noqa: F401

NOW = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)


def policy(**changes):
    return ChatSloPolicy(**{**dict(version="fixture-v1", effective_from=NOW-timedelta(days=1), deadline_seconds=120,
                                   first_token_seconds=20, availability_target=.995), **changes})


def task(**changes):
    return {**dict(id="turn-slo", kb_name="kb", created_at=(NOW-timedelta(seconds=30)).isoformat(),
                   first_at=(NOW-timedelta(seconds=15)).isoformat(), status="completed", finished_at=NOW.isoformat()), **changes}


def slo(q):
    return ChatSloStore(q.store.db_path if q.store.backend == "sqlite" else None, initialize=True)


def window(store):
    return next(w for w in store.windows(now=NOW) if w["workload"] == "kb_chat" and w["window"] == "5m")


def test_durable_terminal_slo_is_not_attempt_counted(quality):
    store = slo(quality)
    for _attempt in range(3):
        store.observe_task(task(), policy(), now=NOW)
    assert window(store)["eligible"] == 1
    assert window(store)["bad"] == 0
    assert window(store)["first_bad"] == 0


def test_missing_first_and_terminal_deadline_remain_bad_after_late_success(quality):
    store = slo(quality)
    original = task(status="running", created_at=(NOW-timedelta(seconds=150)).isoformat(), first_at=None, finished_at=None)
    store.observe_task(original, policy(), now=NOW)
    assert window(store)["bad"] == 1
    assert window(store)["first_bad"] == 1
    store.observe_task({**original, "status": "completed", "first_at": NOW.isoformat(), "finished_at": NOW.isoformat()}, policy(), now=NOW)
    assert window(store)["eligible"] == 1 and window(store)["bad"] == 1
    assert window(store)["burn_rate"] == pytest.approx(200)


def test_early_user_cancel_excluded_but_late_cancel_keeps_violation(quality):
    store = slo(quality)
    early = task(id="early", status="cancelled", created_at=(NOW-timedelta(seconds=15)).isoformat(), first_at=None)
    store.observe_task(early, policy(), now=NOW)
    assert window(store)["eligible"] == 0 and window(store)["excluded"] == 1
    late = task(id="late", status="cancelled", created_at=(NOW-timedelta(seconds=130)).isoformat(), first_at=None)
    store.observe_task(late, policy(), now=NOW)
    assert window(store)["bad"] == 1 and window(store)["first_bad"] == 1
    assert window(store)["excluded"] == 1


def test_first_token_violation_records_active_task_and_no_traffic_is_unknown(quality):
    store = slo(quality)
    assert window(store)["burn_rate"] is None and window(store)["coverage"] == "insufficient_traffic"
    store.observe_task(task(status="running", finished_at=None, first_at=None), policy(), now=NOW)
    assert window(store)["first_bad"] == 1 and window(store)["pending"] == 1
    assert window(store)["eligible"] == 0


def test_policy_versions_are_frozen_for_accepted_tasks(quality):
    store = slo(quality)
    unfinished = task(status="running", created_at=(NOW-timedelta(seconds=150)).isoformat(), first_at=None, finished_at=None)
    store.observe_task(unfinished, policy(), now=NOW)
    with pytest.raises(ValueError, match="immutable"):
        store.observe_task(unfinished, policy(deadline_seconds=300), now=NOW)
    store.observe_task(unfinished, policy(version="fixture-v2", effective_from=NOW, deadline_seconds=300), now=NOW)
    assert window(store)["bad"] == 1


def test_completed_task_without_first_token_is_a_first_token_violation(quality):
    store = slo(quality)
    store.observe_task(task(created_at=(NOW-timedelta(seconds=10)).isoformat(), first_at=None), policy(), now=NOW)
    assert window(store)["eligible"] == 1 and window(store)["bad"] == 0
    assert window(store)["first_bad"] == 1


def test_reconcile_reads_latest_authoritative_terminal_not_stale_candidate(quality):
    q = quality
    store = slo(q)
    from datetime import datetime

    accepted = datetime.fromisoformat(q.turn.created_at.replace("Z", "+00:00"))
    current_policy = policy(effective_from=accepted-timedelta(seconds=1))
    stale = task(id=q.turn.id, created_at=q.turn.created_at, status="running", first_at=None, finished_at=None)
    store.observe_task(stale, current_policy, now=accepted+timedelta(seconds=180), authoritative=True)
    measured = next(w for w in store.windows(now=accepted+timedelta(seconds=180)) if w["workload"] == "kb_chat" and w["window"] == "5m")
    assert measured["bad"] == 0 and measured["pending"] == 0
