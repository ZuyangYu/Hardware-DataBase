"""P0-003: same-session ordering, lease, fencing and stale-owner rejection."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from contextlib import closing

import src.settings
from src.core.conversation import ConversationService, StaleTurnOwner
from src.observability import metrics as obs_metrics


def _seed_users(db_path: str) -> None:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                department_id INTEGER
            )
            """
        )
        conn.execute("INSERT OR IGNORE INTO users (id, username, department_id) VALUES (1, 'u1', NULL)")
        conn.commit()


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._original = {
            "AUTH_DB_PATH": src.settings.AUTH_DB_PATH,
            "CONVERSATION_SERIALIZE_SAME_THREAD": getattr(
                src.settings, "CONVERSATION_SERIALIZE_SAME_THREAD", True
            ),
        }
        self.db_path = os.path.join(self.tmp.name, "auth.db")
        src.settings.AUTH_DB_PATH = self.db_path
        src.settings.CONVERSATION_SERIALIZE_SAME_THREAD = True
        self.addCleanup(self._restore_settings)
        _seed_users(self.db_path)
        self.conv = ConversationService(self.db_path)
        self.session = self.conv.create_session(1, "__general__")

    def _restore_settings(self):
        for key, value in self._original.items():
            setattr(src.settings, key, value)

    def _turn(self, query: str):
        return self.conv.create_turn(user_id=1, session_id=self.session.id, query=query)

    def _expire_session_lease(self, turn_id: str):
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                "UPDATE conversation_execution_state SET lease_expires_at = '2000-01-01T00:00:00+00:00'"
                " WHERE active_turn_id = ?",
                (turn_id,),
            )
            conn.execute(
                "UPDATE chat_turns SET worker_heartbeat_at = '2000-01-01T00:00:00+00:00' WHERE id = ?",
                (turn_id,),
            )
            conn.commit()

    def _execution_row(self) -> sqlite3.Row | None:
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            return conn.execute(
                "SELECT * FROM conversation_execution_state WHERE session_id = ?",
                (self.session.id,),
            ).fetchone()


class ChatTurnOrderingTests(_Base):
    def test_later_turn_is_queued_while_earlier_pending(self):
        first = self._turn("q1")
        second = self._turn("q2")
        third = self._turn("q3")
        self.assertIsNone(self.conv.claim_turn(1, second.id, "worker-b"))
        self.assertIsNone(self.conv.claim_turn(1, third.id, "worker-b"))
        claimed = self.conv.claim_turn(1, first.id, "worker-a")
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.turn_sequence, 1)
        self.assertEqual(claimed.fencing_token, 1)

    def test_claim_assigns_lease_and_second_claim_is_refused(self):
        turn = self._turn("q1")
        claimed = self.conv.claim_turn(1, turn.id, "worker-a")
        self.assertIsNotNone(claimed)
        row = self._execution_row()
        self.assertEqual(row["active_turn_id"], turn.id)
        self.assertEqual(row["lease_owner"], "worker-a")
        self.assertEqual(int(row["fencing_token"]), 1)
        self.assertIsNotNone(row["lease_expires_at"])
        self.assertIsNone(self.conv.claim_turn(1, turn.id, "worker-b"))

    def test_completion_keeps_lease_until_release_then_allows_next_turn(self):
        first = self._turn("q1")
        second = self._turn("q2")
        claimed = self.conv.claim_turn(1, first.id, "worker-a")
        completed = self.conv.complete_turn(
            1,
            first.id,
            "answer",
            {},
            worker_id="worker-a",
            fencing_token=claimed.fencing_token,
            expected_generation=claimed.session_generation,
        )
        self.assertEqual(completed.status, "completed")
        # The lease is held until the owning run releases it (final events
        # still need ownership), then the next turn becomes claimable.
        row = self._execution_row()
        self.assertEqual(row["active_turn_id"], first.id)
        self.assertTrue(
            self.conv.release_turn_ownership(first.id, "worker-a", fencing_token=claimed.fencing_token)
        )
        row = self._execution_row()
        self.assertEqual(row["active_turn_id"], "")
        self.assertEqual(row["lease_owner"], "")
        next_claim = self.conv.claim_turn(1, second.id, "worker-b")
        self.assertIsNotNone(next_claim)
        self.assertEqual(next_claim.fencing_token, 2)

    def test_terminal_leftover_lease_is_reclaimed_by_next_claim(self):
        first = self._turn("q1")
        second = self._turn("q2")
        claimed = self.conv.claim_turn(1, first.id, "worker-a")
        self.conv.complete_turn(
            1,
            first.id,
            "answer",
            {},
            worker_id="worker-a",
            fencing_token=claimed.fencing_token,
            expected_generation=claimed.session_generation,
        )
        # Simulate a crash before release: the completed turn still holds the
        # session lease, but the next claim must reclaim it.
        next_claim = self.conv.claim_turn(1, second.id, "worker-b")
        self.assertIsNotNone(next_claim)
        self.assertEqual(next_claim.fencing_token, 2)

    def test_touch_renews_only_for_current_owner(self):
        turn = self._turn("q1")
        claimed = self.conv.claim_turn(1, turn.id, "worker-a")
        self.assertTrue(self.conv.touch_turn_worker(turn.id, "worker-a", fencing_token=claimed.fencing_token))
        self.assertFalse(self.conv.touch_turn_worker(turn.id, "worker-a", fencing_token=99))
        self.assertFalse(self.conv.touch_turn_worker(turn.id, "worker-b", fencing_token=claimed.fencing_token))

    def test_legacy_mode_keeps_claiming_without_ordering(self):
        src.settings.CONVERSATION_SERIALIZE_SAME_THREAD = False
        first = self._turn("q1")
        second = self._turn("q2")
        claimed = self.conv.claim_turn(1, second.id, "worker-b")
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.fencing_token, 0)
        self.assertTrue(self.conv.touch_turn_worker(second.id, "worker-b"))
        self.conv.complete_turn(1, second.id, "answer", {})
        self.assertIsNotNone(self.conv.claim_turn(1, first.id, "worker-a"))

    def test_requeue_stale_releases_session_lease(self):
        turn = self._turn("q1")
        first_claim = self.conv.claim_turn(1, turn.id, "worker-a")
        self.assertIsNotNone(first_claim)
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                "UPDATE chat_turns SET worker_heartbeat_at = '2000-01-01T00:00:00+00:00' WHERE id = ?",
                (turn.id,),
            )
            conn.commit()
        requeued = self.conv.requeue_stale_turns(stale_after_seconds=30)
        self.assertEqual(requeued, 1)
        row = self._execution_row()
        self.assertEqual(row["active_turn_id"], "")
        takeover = self.conv.claim_turn(1, turn.id, "worker-b")
        self.assertIsNotNone(takeover)
        self.assertEqual(takeover.fencing_token, 2)


class ChatTurnStructuredClaimTests(_Base):
    def test_structured_claim_reports_blocked_and_cancelled(self):
        first = self._turn("q1")
        second = self._turn("q2")
        blocked = self.conv.claim_turn_result(1, second.id, "worker-b")
        self.assertEqual(blocked.status, "blocked")
        self.assertEqual(blocked.blocker_turn_id, first.id)
        self.assertIsNone(blocked.turn)
        claimed = self.conv.claim_turn_result(1, first.id, "worker-a")
        self.assertEqual(claimed.status, "claimed")
        self.assertIsNotNone(claimed.turn)
        self.conv.request_turn_cancel(1, second.id)
        cancelled = self.conv.claim_turn_result(1, second.id, "worker-b")
        self.assertEqual(cancelled.status, "cancelled")
        self.assertEqual(self.conv.claim_turn_result(1, "missing-turn", "worker-b").status, "noop")


class ChatTurnFencingTests(_Base):
    def test_takeover_increments_token_and_rejects_stale_writes(self):
        turn = self._turn("q1")
        first = self.conv.claim_turn(1, turn.id, "worker-a")
        self.assertEqual(first.fencing_token, 1)
        self._expire_session_lease(turn.id)
        second = self.conv.claim_turn(1, turn.id, "worker-b")
        self.assertIsNotNone(second)
        self.assertEqual(second.fencing_token, 2)
        with self.assertRaises(StaleTurnOwner):
            self.conv.append_turn_event(
                turn.id,
                "delta",
                {"text": "late"},
                worker_id="worker-a",
                fencing_token=first.fencing_token,
            )
        with self.assertRaises(StaleTurnOwner):
            self.conv.complete_turn(
                1,
                turn.id,
                "late answer",
                {},
                worker_id="worker-a",
                fencing_token=first.fencing_token,
                expected_generation=first.session_generation,
            )
        accepted = self.conv.append_turn_event(
            turn.id,
            "delta",
            {"text": "current"},
            worker_id="worker-b",
            fencing_token=second.fencing_token,
        )
        self.assertEqual(accepted.seq, 1)
        completed = self.conv.complete_turn(
            1,
            turn.id,
            "answer",
            {},
            worker_id="worker-b",
            fencing_token=second.fencing_token,
            expected_generation=second.session_generation,
        )
        self.assertEqual(completed.status, "completed")

    def test_cancel_revokes_completion_and_allows_cancel_finalize(self):
        turn = self._turn("q1")
        claimed = self.conv.claim_turn(1, turn.id, "worker-a")
        self.assertIsNotNone(claimed)
        cancelled = self.conv.request_turn_cancel(1, turn.id)
        self.assertEqual(cancelled.status, "cancelling")
        with self.assertRaises(StaleTurnOwner):
            self.conv.complete_turn(
                1,
                turn.id,
                "answer",
                {},
                worker_id="worker-a",
                fencing_token=claimed.fencing_token,
                expected_generation=claimed.session_generation,
            )
        finalised = self.conv.fail_turn(
            1,
            turn.id,
            "已停止生成",
            cancelled=True,
            worker_id="worker-a",
            fencing_token=claimed.fencing_token,
        )
        self.assertEqual(finalised.status, "cancelled")
        self.assertTrue(
            self.conv.release_turn_ownership(turn.id, "worker-a", fencing_token=claimed.fencing_token)
        )
        row = self._execution_row()
        self.assertEqual(row["active_turn_id"], "")
        with closing(sqlite3.connect(self.db_path)) as conn:
            generation = conn.execute(
                "SELECT generation FROM conversation_execution_state WHERE session_id = ?",
                (self.session.id,),
            ).fetchone()[0]
        self.assertEqual(int(generation), 1)

    def test_stale_write_counter_is_recorded(self):
        turn = self._turn("q1")
        first = self.conv.claim_turn(1, turn.id, "worker-a")
        self._expire_session_lease(turn.id)
        second = self.conv.claim_turn(1, turn.id, "worker-b")
        self.assertIsNotNone(second)
        original = obs_metrics.counter

        calls: list[tuple[str, dict]] = []

        def spy(name, *, attributes=None, value=1, description=""):
            calls.append((name, dict(attributes or {})))
            return original(name, attributes=attributes, value=value, description=description)

        obs_metrics.counter = spy
        try:
            with self.assertRaises(StaleTurnOwner):
                self.conv.append_turn_event(
                    turn.id,
                    "delta",
                    {"text": "late"},
                    worker_id="worker-a",
                    fencing_token=first.fencing_token,
                )
        finally:
            obs_metrics.counter = original
        self.assertIn(
            ("hdb.conversation.stale_write_rejected", {"operation": "append_event"}),
            calls,
        )


class ChatTurnTerminalEventTests(_Base):
    """v3 §4.7: terminal events commit atomically with the terminal status."""

    def test_complete_turn_persists_terminal_events_in_the_same_transaction(self):
        turn = self._turn("q1")
        claimed = self.conv.claim_turn(1, turn.id, "worker-a")

        def builder(completed):
            return [
                ("done", {"answer": completed.answer, "status": completed.status}),
                ("turn_end", {"status": "completed"}),
            ]

        completion = self.conv.complete_turn_result(
            1,
            turn.id,
            "answer",
            {},
            worker_id="worker-a",
            fencing_token=claimed.fencing_token,
            expected_generation=claimed.session_generation,
            terminal_events_builder=builder,
        )
        self.assertEqual(completion.turn.status, "completed")
        self.assertEqual([event.seq for event in completion.terminal_events], [1, 2])
        persisted = self.conv.list_turn_events(1, turn.id)
        self.assertEqual([event.event_type for event in persisted], ["done", "turn_end"])
        self.assertEqual(persisted[0].payload["answer"], "answer")
        self.assertEqual(self.conv.get_turn(1, turn.id).last_event_seq, 2)

    def test_complete_turn_rolls_back_answer_when_terminal_event_builder_fails(self):
        turn = self._turn("q1")
        claimed = self.conv.claim_turn(1, turn.id, "worker-a")

        def broken(_completed):
            raise RuntimeError("terminal event build failed")

        with self.assertRaises(RuntimeError):
            self.conv.complete_turn_result(
                1,
                turn.id,
                "answer",
                {},
                worker_id="worker-a",
                fencing_token=claimed.fencing_token,
                expected_generation=claimed.session_generation,
                terminal_events_builder=broken,
            )
        current = self.conv.get_turn(1, turn.id)
        self.assertEqual(current.status, "streaming")
        self.assertEqual(current.answer, "")
        self.assertEqual(current.last_event_seq, 0)
        self.assertEqual(self.conv.list_turn_events(1, turn.id), [])
        messages = self.conv.list_messages(1, self.session.id)
        self.assertEqual(messages[-1].content, "")

    def test_fail_turn_persists_error_events_in_the_same_transaction(self):
        turn = self._turn("q1")
        claimed = self.conv.claim_turn(1, turn.id, "worker-a")

        def builder(_final):
            return [
                ("error", {"message": "boom"}),
                ("turn_end", {"status": "failed"}),
            ]

        finalization = self.conv.fail_turn_result(
            1,
            turn.id,
            "boom",
            worker_id="worker-a",
            fencing_token=claimed.fencing_token,
            terminal_events_builder=builder,
        )
        self.assertEqual(finalization.turn.status, "failed")
        self.assertEqual(
            [event.event_type for event in self.conv.list_turn_events(1, turn.id)],
            ["error", "turn_end"],
        )
        self.assertEqual(self.conv.get_turn(1, turn.id).status, "failed")

    def test_requeue_stale_turn_converges_cancelled_request(self):
        turn = self._turn("q1")
        self.conv.claim_turn(1, turn.id, "worker-a")
        cancelled = self.conv.request_turn_cancel(1, turn.id)
        self.assertEqual(cancelled.status, "cancelling")
        self._expire_session_lease(turn.id)
        requeued = self.conv.requeue_stale_turns(stale_after_seconds=30)
        self.assertEqual(requeued, 0)
        current = self.conv.get_turn(1, turn.id)
        self.assertEqual(current.status, "cancelled")
        self.assertEqual(
            [event.event_type for event in self.conv.list_turn_events(1, turn.id)],
            ["error", "turn_end"],
        )
        row = self._execution_row()
        self.assertEqual(row["active_turn_id"], "")

    def test_requeue_stale_turn_fails_exhausted_retry(self):
        turn = self._turn("q1")
        self.conv.claim_turn(1, turn.id, "worker-a")
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute("UPDATE chat_turns SET retry_count = 3 WHERE id = ?", (turn.id,))
            conn.commit()
        self._expire_session_lease(turn.id)
        requeued = self.conv.requeue_stale_turns(stale_after_seconds=30)
        self.assertEqual(requeued, 0)
        current = self.conv.get_turn(1, turn.id)
        self.assertEqual(current.status, "failed")
        self.assertIn("重试", current.error_message)
        self.assertEqual(
            [event.event_type for event in self.conv.list_turn_events(1, turn.id)],
            ["error", "turn_end"],
        )

    def test_requeue_stale_turn_requeues_live_claim(self):
        turn = self._turn("q1")
        self.conv.claim_turn(1, turn.id, "worker-a")
        self._expire_session_lease(turn.id)
        requeued = self.conv.requeue_stale_turns(stale_after_seconds=30)
        self.assertEqual(requeued, 1)
        self.assertEqual(self.conv.get_turn(1, turn.id).status, "pending")
        self.assertEqual(self.conv.list_turn_events(1, turn.id), [])

    def test_claim_converges_stale_cancelled_turn(self):
        turn = self._turn("q1")
        self.conv.claim_turn(1, turn.id, "worker-a")
        self.conv.request_turn_cancel(1, turn.id)
        self._expire_session_lease(turn.id)
        result = self.conv.claim_turn_result(1, turn.id, "worker-b", stale_after_seconds=30)
        self.assertEqual(result.status, "cancelled")
        current = self.conv.get_turn(1, turn.id)
        self.assertEqual(current.status, "cancelled")
        self.assertEqual(
            [event.event_type for event in self.conv.list_turn_events(1, turn.id)],
            ["error", "turn_end"],
        )


if __name__ == "__main__":
    unittest.main()
