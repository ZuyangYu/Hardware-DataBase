"""§13.2: real multi-process SQLite contention for the P0a/P0b guarantees.

Pure in-process unit tests cannot prove the cross-process behavior of the
turn claim / coordinator lease / quota store on a shared SQLite file.  These
tests spawn real subprocesses that contend on one database and assert:

1. same-session turn ordering + fencing: only the earliest turn is claimable,
   exactly one process wins, the token is 1, later turns stay blocked;
2. ``coordinator_leases`` elects exactly one owner, with token 1;
3. quota permits from ``resource_quotas.db`` are mutually exclusive across
   processes (a start/end log may never contain two overlapping holders).
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _seed(db_path: str) -> None:
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


def _spawn(script: str, env_extra: dict[str, str]) -> subprocess.Popen:
    env = dict(os.environ)
    env.update(env_extra)
    env["PYTHONPATH"] = str(REPO_ROOT)
    return subprocess.Popen(
        [sys.executable, "-c", script],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _wait_ready(stop_file: str, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while not os.path.exists(stop_file):
        if time.monotonic() > deadline:
            raise TimeoutError("children never signalled ready")
        time.sleep(0.02)


def _finish(proc: subprocess.Popen, timeout: float = 60.0) -> dict:
    out, err = proc.communicate(timeout=timeout)
    if proc.returncode != 0:
        raise AssertionError(f"child failed rc={proc.returncode}\nstdout={out}\nstderr={err}")
    lines = [line for line in out.splitlines() if line.strip().startswith("{")]
    if not lines:
        raise AssertionError(f"child produced no JSON payload\nstdout={out}\nstderr={err}")
    return json.loads(lines[-1])


_CLAIM_SCRIPT = """
import json, os, sys, time
sys.path.insert(0, os.environ["PYTHONPATH"])
import src.settings
src.settings.AUTH_DB_PATH = os.environ["HDB_TEST_DB"]
src.settings.CONVERSATION_SERIALIZE_SAME_THREAD = True
from src.core.conversation import ConversationService
open(os.environ["HDB_TEST_READY"], "w").close()
while not os.path.exists(os.environ["HDB_TEST_GO"]):
    time.sleep(0.005)
conv = ConversationService(os.environ["HDB_TEST_DB"])
turn = conv.claim_turn(int(os.environ["HDB_TEST_USER"]), os.environ["HDB_TEST_TURN"], os.environ["HDB_TEST_WORKER"])
print(json.dumps({
    "worker": os.environ["HDB_TEST_WORKER"],
    "turn": turn.id if turn is not None else None,
    "token": int(getattr(turn, "fencing_token", 0) or 0),
}))
"""


class MultiProcessTurnClaimTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = os.path.join(self.tmp.name, "auth.db")
        _seed(self.db_path)
        from src.core.conversation import ConversationService

        conv = ConversationService(self.db_path)
        self.session = conv.create_session(1, "__general__")
        self.first = conv.create_turn(user_id=1, session_id=self.session.id, query="q1")
        self.second = conv.create_turn(user_id=1, session_id=self.session.id, query="q2")

    def test_cross_process_claim_orders_and_fences(self):
        ready_dir = os.path.join(self.tmp.name, "ready")
        os.makedirs(ready_dir, exist_ok=True)
        go_file = os.path.join(self.tmp.name, "go")
        specs = []
        for index, (turn, worker) in enumerate(
            (
                (self.first.id, "w-a"),
                (self.first.id, "w-b"),
                (self.second.id, "w-c"),
                (self.second.id, "w-d"),
            )
        ):
            ready_file = os.path.join(ready_dir, f"ready-{index}")
            proc = _spawn(
                _CLAIM_SCRIPT,
                {
                    "HDB_TEST_DB": self.db_path,
                    "HDB_TEST_USER": "1",
                    "HDB_TEST_TURN": turn,
                    "HDB_TEST_WORKER": worker,
                    "HDB_TEST_READY": ready_file,
                    "HDB_TEST_GO": go_file,
                },
            )
            specs.append((proc, ready_file))
        self.addCleanup(lambda: [proc.kill() for proc, _ in specs if proc.poll() is None])
        for _, ready_file in specs:
            _wait_ready(ready_file)
        open(go_file, "w").close()
        results = [_finish(proc) for proc, _ in specs]
        first_winners = [r for r in results if r["turn"] == self.first.id]
        later_winners = [r for r in results if r["turn"] == self.second.id]
        self.assertEqual(len(first_winners), 1, results)
        self.assertEqual(first_winners[0]["token"], 1)
        self.assertEqual(later_winners, [], "later turn must stay blocked")


_LEASE_SCRIPT = """
import json, os, sys, time
sys.path.insert(0, os.environ["PYTHONPATH"])
import src.settings
src.settings.CHAT_ATTACHMENT_INDEX_DB_PATH = os.environ["HDB_TEST_LEASE_DB"]
from src.attachments.coordinator_lease import CoordinatorLeaseStore
open(os.environ["HDB_TEST_READY"], "w").close()
while not os.path.exists(os.environ["HDB_TEST_GO"]):
    time.sleep(0.005)
store = CoordinatorLeaseStore(db_path=os.environ["HDB_TEST_LEASE_DB"])
token = store.try_acquire(os.environ["HDB_TEST_WORKER"], 30)
print(json.dumps({"worker": os.environ["HDB_TEST_WORKER"], "token": token}))
"""


class MultiProcessCoordinatorLeaseTests(unittest.TestCase):
    def test_exactly_one_process_wins_the_lease(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = os.path.join(tmp.name, "attachments.db")
        ready_dir = os.path.join(tmp.name, "ready")
        os.makedirs(ready_dir, exist_ok=True)
        go_file = os.path.join(tmp.name, "go")
        specs = []
        for index in range(4):
            ready_file = os.path.join(ready_dir, f"ready-{index}")
            proc = _spawn(
                _LEASE_SCRIPT,
                {
                    "HDB_TEST_LEASE_DB": db_path,
                    "HDB_TEST_WORKER": f"lease-{index}",
                    "HDB_TEST_READY": ready_file,
                    "HDB_TEST_GO": go_file,
                },
            )
            specs.append((proc, ready_file))
        self.addCleanup(lambda: [proc.kill() for proc, _ in specs if proc.poll() is None])
        for _, ready_file in specs:
            _wait_ready(ready_file)
        open(go_file, "w").close()
        results = [_finish(proc) for proc, _ in specs]
        winners = [r for r in results if r["token"] is not None]
        self.assertEqual(len(winners), 1, results)
        self.assertEqual(winners[0]["token"], 1)


_QUOTA_SCRIPT = """
import json, os, sys, time
sys.path.insert(0, os.environ["PYTHONPATH"])
import src.settings
from src.core.concurrency.resource_governor import SqliteQuotaStore
store = SqliteQuotaStore(os.environ["HDB_TEST_QUOTA_DB"], lease_seconds=60, deployment_id="mp")
log_path = os.environ["HDB_TEST_LOG"]
open(os.environ["HDB_TEST_READY"], "w").close()
while not os.path.exists(os.environ["HDB_TEST_GO"]):
    time.sleep(0.005)
worker = os.environ["HDB_TEST_WORKER"]

def log(line):
    with open(log_path, "a", encoding="utf-8") as handle:
        handle.write(line + "\\n")

import sqlite3 as _sqlite3

capacity = int(os.environ.get("HDB_TEST_QUOTA_CAPACITY", "1"))
for _ in range(3):
    permit = None
    deadline = time.monotonic() + 40
    while permit is None and time.monotonic() < deadline:
        try:
            permit = store.try_acquire(
                "llm" if capacity > 1 else "tool", capacity=capacity, reserved_interactive=0, interactive=True,
                owner_id=worker, attempt_id="a",
            )
        except _sqlite3.OperationalError:
            permit = None
        if permit is None:
            time.sleep(0.01)
    if permit is None:
        print(json.dumps({"worker": worker, "ok": False}))
        sys.exit(1)
    log(f"start:{worker}")
    time.sleep(0.15)
    log(f"end:{worker}")
    store.release(permit)
print(json.dumps({"worker": worker, "ok": True}))
"""


class MultiProcessQuotaExclusionTests(unittest.TestCase):
    def test_capacity_one_permits_never_overlap_across_processes(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = os.path.join(tmp.name, "resource_quotas.db")
        log_path = os.path.join(tmp.name, "holders.log")
        ready_dir = os.path.join(tmp.name, "ready")
        os.makedirs(ready_dir, exist_ok=True)
        go_file = os.path.join(tmp.name, "go")
        specs = []
        for index in range(4):
            ready_file = os.path.join(ready_dir, f"ready-{index}")
            proc = _spawn(
                _QUOTA_SCRIPT,
                {
                    "HDB_TEST_QUOTA_DB": db_path,
                    "HDB_TEST_LOG": log_path,
                    "HDB_TEST_WORKER": f"quota-{index}",
                    "HDB_TEST_READY": ready_file,
                    "HDB_TEST_GO": go_file,
                },
            )
            specs.append((proc, ready_file))
        self.addCleanup(lambda: [proc.kill() for proc, _ in specs if proc.poll() is None])
        for _, ready_file in specs:
            _wait_ready(ready_file)
        open(go_file, "w").close()
        results = [_finish(proc) for proc, _ in specs]
        self.assertTrue(all(r["ok"] for r in results), results)
        active: set[str] = set()
        events = [
            line.strip().split(":", 1)
            for line in Path(log_path).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual(len(events), 24, events)
        for action, worker in events:
            if action == "start":
                self.assertNotIn(worker, active)
                self.assertEqual(active, set(), f"overlap: {worker} entered while {active} held")
                active.add(worker)
            else:
                active.discard(worker)
        self.assertEqual(active, set())


class MultiProcessLlmDeploymentTotalTests(unittest.TestCase):
    """G2: deployment LLM total must not be multiplied by process count."""

    def test_capacity_two_llm_permits_never_exceed_two(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = os.path.join(tmp.name, "resource_quotas.db")
        log_path = os.path.join(tmp.name, "holders.log")
        ready_dir = os.path.join(tmp.name, "ready")
        os.makedirs(ready_dir, exist_ok=True)
        go_file = os.path.join(tmp.name, "go")
        specs = []
        for index in range(4):
            ready_file = os.path.join(ready_dir, f"ready-{index}")
            proc = _spawn(
                _QUOTA_SCRIPT,
                {
                    "HDB_TEST_QUOTA_DB": db_path,
                    "HDB_TEST_LOG": log_path,
                    "HDB_TEST_WORKER": f"llm-{index}",
                    "HDB_TEST_READY": ready_file,
                    "HDB_TEST_GO": go_file,
                    "HDB_TEST_QUOTA_CAPACITY": "2",
                },
            )
            specs.append((proc, ready_file))
        self.addCleanup(lambda: [proc.kill() for proc, _ in specs if proc.poll() is None])
        for _, ready_file in specs:
            _wait_ready(ready_file)
        open(go_file, "w").close()
        results = [_finish(proc) for proc, _ in specs]
        self.assertTrue(all(r["ok"] for r in results), results)
        active: set[str] = set()
        events = [
            line.strip().split(":", 1)
            for line in Path(log_path).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual(len(events), 24, events)
        high_water = 0
        for action, worker in events:
            if action == "start":
                active.add(worker)
                high_water = max(high_water, len(active))
            else:
                active.discard(worker)
        self.assertLessEqual(high_water, 2, f"deployment LLM total exceeded: {high_water}")
        self.assertEqual(active, set())


if __name__ == "__main__":
    unittest.main()
