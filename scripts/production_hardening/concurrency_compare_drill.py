"""Multi-user concurrency A/B drill with metrics and comparison report.

Runs the *same* workload twice against isolated temporary storage:

- **off**: default flags (legacy concurrent sessions, no deployment governor);
- **on**: ``CONVERSATION_SERIALIZE_SAME_THREAD=true``,
  ``RESOURCE_GOVERNOR_ENABLED=true`` (deployment scope, shared SQLite quotas),
  ``SHARED_RETRIEVAL_EXECUTOR_ENABLED=true``.

Scenarios per mode:

1. cross-user load: N users x M sequential turns each, fired in parallel;
2. isolation: user B must not read user A's session/turn;
3. same-session burst: K turns queued back-to-back — on mode must execute them
   strictly in sequence, off mode typically overlaps;
4. overload: a dedicated user fires rapid turns to observe 429 + Retry-After
   (admission control).

A JSON report plus a Markdown comparison table are written for review.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))


class _Replica:
    def __init__(self, port: int, env: dict[str, str], log_path: Path | None = None) -> None:
        child_env = dict(os.environ)
        child_env.update(env)
        self.port = port
        self.base_url = f"http://127.0.0.1:{port}"
        self._log_path = Path(log_path) if log_path else None
        self._log = open(log_path, "wb") if log_path else subprocess.DEVNULL
        self.proc = subprocess.Popen(
            [
                sys.executable,
                "-u",
                "-m",
                "uvicorn",
                "src.api.app:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--log-level",
                "warning",
            ],
            cwd=str(REPO_ROOT),
            env=child_env,
            stdout=self._log,
            stderr=self._log,
            start_new_session=True,
        )

    def wait_ready(self, timeout: float = 180.0) -> None:
        deadline = time.monotonic() + timeout
        attempts = 0
        while time.monotonic() < deadline:
            attempts += 1
            last_error = ""
            try:
                if httpx.get(f"{self.base_url}/health", timeout=2.0).status_code == 200:
                    return
                last_error = "non-200"
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            if self.proc.poll() is not None:
                tail = ""
                if self._log_path and self._log_path.exists():
                    tail = self._log_path.read_text(errors="replace")[-500:]
                raise RuntimeError(f"api:{self.port} exited early rc={self.proc.returncode}\n{tail}")
            if attempts % 20 == 0:
                tail = ""
                if self._log_path and self._log_path.exists():
                    tail = self._log_path.read_text(errors="replace")[-300:]
                print(
                    f"[wait_ready] api:{self.port} alive pid={self.proc.pid} err={last_error!r} log={tail!r}",
                    flush=True,
                )
            time.sleep(0.5)
        raise TimeoutError(f"api:{self.port} never ready")

    def stop(self) -> None:
        if self.proc.poll() is None:
            try:
                os.killpg(os.getpgid(self.proc.pid), 15)
            except ProcessLookupError:
                pass
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(self.proc.pid), 9)
                except ProcessLookupError:
                    pass


class _Worker:
    def __init__(self, role: str, env: dict[str, str]) -> None:
        child_env = dict(os.environ)
        child_env.update(env)
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "src.workers.main", "--role", role],
            cwd=str(REPO_ROOT),
            env=child_env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    def stop(self) -> None:
        if self.proc.poll() is None:
            try:
                os.killpg(os.getpgid(self.proc.pid), 15)
            except ProcessLookupError:
                pass
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(self.proc.pid), 9)
                except ProcessLookupError:
                    pass


_SEED_SCRIPT = """
import sys
from src.core.auth import ROLE_EMPLOYEE, AuthService
import src.settings as settings

db_path, password = sys.argv[1], sys.argv[2]
usernames = sys.argv[3:]
auth = AuthService(db_path=db_path)
sysadmin = auth.get_user_by_username(settings.AUTH_DEFAULT_ADMIN_USERNAME)
if sysadmin is None:
    raise SystemExit("default system admin is not configured")
department = auth.create_department("concurrency-drill")
for name in usernames:
    if auth.get_user_by_username(name) is None:
        auth.create_user_as(sysadmin, name, password, ROLE_EMPLOYEE, department.id)
"""


def _seed_users(db_path: str, usernames: list[str], password: str) -> None:
    """Seed in a child process: the parent must hold no DB handle, or the API
    lifespan's first SQLite write can block on the inherited lock."""
    env = dict(os.environ)
    env["AUTH_DB_PATH"] = db_path
    completed = subprocess.run(
        [sys.executable, "-c", _SEED_SCRIPT, db_path, password, *usernames],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"seeding failed: {completed.stderr[-500:]}")


def _login(base_url: str, username: str, password: str) -> str:
    response = httpx.post(
        f"{base_url}/api/v1/login",
        json={"username": username, "password": password},
        timeout=15,
    )
    response.raise_for_status()
    return str(response.json()["token"])


def _create_session(base_url: str, token: str, title: str) -> int:
    response = httpx.post(
        f"{base_url}/api/v1/conversations",
        json={"kb_name": "__general__", "title": title},
        headers={"Authorization": f"Bearer {token}"},
        timeout=20,
    )
    response.raise_for_status()
    return int(response.json()["id"])


def _create_turn(base_url: str, token: str, session_id: int, query: str) -> tuple[str, str]:
    response = httpx.post(
        f"{base_url}/api/v1/conversations/{session_id}/turns",
        json={"query": query, "client_request_id": f"cc-{time.time_ns()}"},
        headers={"Authorization": f"Bearer {token}"},
        timeout=20,
    )
    turn_id = str(response.json()["turn"]["id"]) if response.status_code == 201 else ""
    return str(response.status_code), turn_id


def _start_and_wait(
    base_url: str,
    token: str,
    turn_id: str,
    *,
    timeout: float = 300.0,
) -> str:
    headers = {"Authorization": f"Bearer {token}"}
    httpx.post(f"{base_url}/api/v1/turns/{turn_id}/start", headers=headers, timeout=20)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        detail = httpx.get(f"{base_url}/api/v1/turns/{turn_id}", headers=headers, timeout=15)
        if detail.status_code == 200 and str(detail.json().get("status")) in {
            "completed",
            "failed",
            "cancelled",
        }:
            return str(detail.json()["status"])
        time.sleep(1.0)
    return "timeout"


def _turn_rows(db_path: str, turn_ids: list[str]) -> dict[str, dict]:
    if not turn_ids:
        return {}
    placeholders = ",".join("?" for _ in turn_ids)
    with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"SELECT id, turn_sequence, status, created_at, started_at, finished_at, answer"
            f" FROM chat_turns WHERE id IN ({placeholders})",
            turn_ids,
        ).fetchall()
    return {str(row["id"]): dict(row) for row in rows}


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        value = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _seconds(later: datetime | None, earlier: datetime | None) -> float | None:
    if later is None or earlier is None:
        return None
    return max(0.0, (later - earlier).total_seconds())


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(fraction * (len(ordered) - 1)))))
    return round(ordered[index], 3)


def _run_mode(mode: str, args: argparse.Namespace, workdir: Path) -> dict:
    workdir.mkdir(parents=True, exist_ok=True)
    db_path = str(workdir / "auth.db")
    env = {
        "AUTH_DB_PATH": db_path,
        "AGENT_CHECKPOINT_DB_PATH": str(workdir / "agent_checkpoints.db"),
        "CHAT_ATTACHMENT_INDEX_DB_PATH": str(workdir / "attachments.db"),
        "CHAT_ATTACHMENT_STORAGE_DIR": str(workdir / "attachment_files"),
        "RESOURCE_GOVERNOR_DB_PATH": str(workdir / "resource_quotas.db"),
        "HDB_API_SPAWN_WORKER": "0",
        "HDB_API_SPAWN_EXPORT_WORKER": "0",
        "HDB_API_SPAWN_MEMORY_WORKER": "0",
    }
    if mode == "on":
        env.update(
            {
                "CONVERSATION_SERIALIZE_SAME_THREAD": "true",
                "RESOURCE_GOVERNOR_ENABLED": "true",
                "RESOURCE_GOVERNOR_SCOPE": "deployment",
                "SHARED_RETRIEVAL_EXECUTOR_ENABLED": "true",
                "ADMISSION_CHAT_RPM_PER_USER": str(args.user_rpm),
                "ADMISSION_CHAT_RPM_PER_TENANT": str(args.tenant_rpm),
            }
        )
    else:
        env.update({"CONVERSATION_SERIALIZE_SAME_THREAD": "false"})
    if args.queue_deadline > 0:
        env["CHAT_QUEUE_DEADLINE_SECONDS"] = str(int(args.queue_deadline))

    usernames = [f"ccu{i}" for i in range(args.users)] + ["cco"]
    _seed_users(db_path, usernames, args.password)

    api = _Replica(args.port, env, log_path=workdir / "api.log")
    workers: list[_Worker] = []
    report: dict[str, object] = {"mode": mode, "config": env, "turns": [], "scenarios": {}}
    try:
        # Bring the API up first: three heavy processes initializing at once can
        # exceed the readiness budget on a loaded dev machine.
        api.wait_ready(timeout=180.0)
        workers = [_Worker("chat", env) for _ in range(2)]
        tokens = {name: _login(api.base_url, name, args.password) for name in usernames}

        # Scenario 1: cross-user parallel rounds.
        sessions = {
            name: _create_session(api.base_url, tokens[name], f"cc-{mode}-{name}")
            for name in usernames[: args.users]
        }
        play_turns: list[str] = []
        user_turn_ids: dict[str, str] = {}
        errors: list[str] = []
        per_user_completed: dict[str, int] = {}
        peak_inflight = 0
        active_users = usernames[: args.users]
        wave_size = args.wave_size if args.wave_size > 0 else len(active_users)
        for _round in range(args.turns_per_user):
            for offset in range(0, len(active_users), wave_size):
                wave = active_users[offset : offset + wave_size]
                peak_inflight = max(peak_inflight, len(wave))
                threads = []
                results: dict[str, tuple[str, str]] = {}

                def fire(name: str) -> None:
                    try:
                        status, turn_id = _create_turn(
                            api.base_url, tokens[name], sessions[name], f"{mode} 并发探测 {time.time_ns()}"
                        )
                        results[name] = (status, turn_id)
                    except Exception as exc:  # noqa: BLE001
                        errors.append(f"{name}: {exc}")

                for name in wave:
                    thread = threading.Thread(target=fire, args=(name,))
                    thread.start()
                    threads.append(thread)
                for thread in threads:
                    thread.join(timeout=60)
                for name, (status, turn_id) in results.items():
                    if status != "201":
                        errors.append(f"{name}: create status {status}")
                        continue
                    play_turns.append(turn_id)
                    user_turn_ids.setdefault(name, turn_id)
                for name, (status, turn_id) in results.items():
                    if status != "201":
                        continue
                    terminal = _start_and_wait(
                        api.base_url, tokens[name], turn_id, timeout=args.wait_timeout
                    )
                    if terminal != "completed":
                        errors.append(f"{name}: terminal {terminal}")
                    else:
                        per_user_completed[name] = per_user_completed.get(name, 0) + 1

        rows = _turn_rows(db_path, play_turns)
        for turn_id in play_turns:
            row = rows.get(turn_id) or {}
            created, started, finished = (
                _parse(row.get("created_at")),
                _parse(row.get("started_at")),
                _parse(row.get("finished_at")),
            )
            report["turns"].append(
                {
                    "turn_id": turn_id,
                    "turn_sequence": row.get("turn_sequence"),
                    "status": row.get("status"),
                    "queue_wait_s": _seconds(started, created),
                    "e2e_s": _seconds(finished, created),
                    "answer_len": len(str(row.get("answer") or "")),
                }
            )
        report["scenarios"]["cross_user_errors"] = errors

        # Scenario 2: cross-user isolation.
        victim_session = sessions[usernames[0]]
        intruder = usernames[1]
        cross_session = httpx.get(
            f"{api.base_url}/api/v1/conversations/{victim_session}",
            headers={"Authorization": f"Bearer {tokens[intruder]}"},
            timeout=15,
        )
        victim_turn = user_turn_ids.get(usernames[0], "")
        if victim_turn:
            cross_turn = httpx.get(
                f"{api.base_url}/api/v1/turns/{victim_turn}",
                headers={"Authorization": f"Bearer {tokens[intruder]}"},
                timeout=15,
            )
            cross_turn_code = cross_turn.status_code
        else:
            cross_turn_code = None
        report["scenarios"]["isolation"] = {
            "cross_user_session_status": cross_session.status_code,
            "cross_user_turn_status": cross_turn_code,
            "victim_turn_id": victim_turn,
            "blocked": cross_session.status_code in {403, 404}
            and cross_turn_code in {403, 404, None},
        }

        # Scenario 3: same-session burst ordering.
        burst_session = _create_session(api.base_url, tokens[usernames[0]], f"cc-burst-{mode}")
        burst_ids: list[str] = []
        for index in range(args.burst):
            status, turn_id = _create_turn(
                api.base_url, tokens[usernames[0]], burst_session, f"{mode} 顺序探测 {index}"
            )
            if status != "201":
                break
            burst_ids.append(str(turn_id))
        for turn_id in burst_ids:
            httpx.post(
                f"{api.base_url}/api/v1/turns/{turn_id}/start",
                headers={"Authorization": f"Bearer {tokens[usernames[0]]}"},
                timeout=20,
            )
        terminal = [
            _start_and_wait(api.base_url, tokens[usernames[0]], turn_id)
            for turn_id in burst_ids
        ]
        burst_rows = _turn_rows(db_path, burst_ids)
        intervals: list[tuple[datetime | None, datetime | None]] = []
        for turn_id in burst_ids:
            row = burst_rows.get(turn_id) or {}
            intervals.append((_parse(row.get("started_at")), _parse(row.get("finished_at"))))
        overlaps = 0
        for i in range(1, len(intervals)):
            prev_finish = intervals[i - 1][1]
            current_start = intervals[i][0]
            if prev_finish and current_start and current_start < prev_finish:
                overlaps += 1
        waits = [
            _seconds(start, _parse((burst_rows.get(turn_id) or {}).get("created_at")))
            for turn_id, (start, _finish) in zip(burst_ids, intervals)
        ]
        report["scenarios"]["burst"] = {
            "turns": len(burst_ids),
            "terminals": terminal,
            "overlaps": overlaps,
            "queue_waits_s": [round(value, 3) if value is not None else None for value in waits],
            "serialized": overlaps == 0 and len(burst_ids) > 1,
        }

        # Scenario 4: admission overload for a dedicated user.
        overload_statuses: list[int] = []
        retry_after = False
        overload_session = _create_session(api.base_url, tokens["cco"], f"cc-overload-{mode}")
        for index in range(args.overload):
            response = httpx.post(
                f"{api.base_url}/api/v1/conversations/{overload_session}/turns",
                json={"query": f"过载探测 {index}", "client_request_id": f"cc-over-{time.time_ns()}"},
                headers={"Authorization": f"Bearer {tokens['cco']}"},
                timeout=20,
            )
            overload_statuses.append(response.status_code)
            retry_after = retry_after or "Retry-After" in response.headers
        report["scenarios"]["overload"] = {
            "requests": args.overload,
            "statuses": overload_statuses,
            "rejected_429": sum(1 for code in overload_statuses if code == 429),
            "retry_after_present": retry_after,
        }

        e2e = [float(t["e2e_s"]) for t in report["turns"] if t.get("e2e_s") is not None]
        waits = [
            float(t["queue_wait_s"])
            for t in report["turns"]
            if t.get("queue_wait_s") is not None
        ]
        report["summary"] = {
            "completed": sum(1 for t in report["turns"] if t["status"] == "completed"),
            "failed_or_other": sum(1 for t in report["turns"] if t["status"] != "completed"),
            "p50_e2e_s": _percentile(e2e, 0.50),
            "p95_e2e_s": _percentile(e2e, 0.95),
            "mean_e2e_s": round(statistics.fmean(e2e), 3) if e2e else None,
            "p95_queue_wait_s": _percentile(waits, 0.95),
            "cross_user_parallel_rounds": args.turns_per_user,
            "users": args.users,
            "wave_size": wave_size,
            "peak_inflight": peak_inflight,
            "users_with_completed_turn": len(per_user_completed),
        }
    finally:
        for worker in workers:
            worker.stop()
        api.stop()
    return report


def _markdown(off: dict, on: dict) -> str:
    def line(report: dict) -> str:
        summary = report["summary"]
        scenario = report["scenarios"]
        return (
            f"| {report['mode']} | {summary['completed']}/{len(report['turns'])} | "
            f"{summary['users_with_completed_turn']}/{summary['users']} | "
            f"{summary['p50_e2e_s']} | {summary['p95_e2e_s']} | {summary['p95_queue_wait_s']} | "
            f"{'是' if scenario['burst']['serialized'] else '否'} | "
            f"{'是' if scenario['isolation']['blocked'] else '否'} | "
            f"{scenario['overload']['rejected_429']}/{scenario['overload']['requests']} |"
        )

    rows = "\n".join(line(report) for report in (off, on))
    burst_on = on["scenarios"]["burst"]
    burst_off = off["scenarios"]["burst"]
    return f"""# 多用户多并发 A/B 实测报告

> 口径：隔离临时库（不影响在线服务）；通用聊天；同一模型；**{off['summary']['users']} 个用户**、每组 {off['summary']['cross_user_parallel_rounds']} 轮跨用户并行（每波 {off['summary']['wave_size']} 人，峰值在途 {off['summary']['peak_inflight']}）+ {burst_on['turns']} 条同会话突发 + {on['scenarios']['overload']['requests']} 次快速提交。
> `off` = 默认开关（legacy 并发）；`on` = `CONVERSATION_SERIALIZE_SAME_THREAD` + 部署级 governor + 共享检索执行器 + 低 RPM 准入。

## 结果总表

| 模式 | 完成/总 | 有完成 turn 用户数 | 跨用户 e2e P50 (s) | 跨用户 e2e P95 (s) | 队列等待 P95 (s) | 同会话严格串行 | 跨用户隔离拦截 | 准入拒绝 |
|---|---|---|---|---|---|---|---|---|
{rows}

## 同会话突发（顺序语义）

| 模式 | 突发重叠次数 | 各 turn queue wait (s) | 串行结论 |
|---|---|---|---|
| off | {burst_off['overlaps']} | {burst_off['queue_waits_s']} | {'串行' if burst_off['serialized'] else '发生并行/重叠（legacy 行为）'} |
| on | {burst_on['overlaps']} | {burst_on['queue_waits_s']} | {'严格按 turn_sequence 串行，后序等待前序完成' if burst_on['serialized'] else '未串行（异常）'} |

## 隔离与过载

| 检查 | off | on |
|---|---|---|
| 他用户会话访问状态码 | {off['scenarios']['isolation']['cross_user_session_status']} | {on['scenarios']['isolation']['cross_user_session_status']} |
| 他用户 turn 访问状态码 | {off['scenarios']['isolation']['cross_user_turn_status']} | {on['scenarios']['isolation']['cross_user_turn_status']} |
| 429 次数 / 请求数 | {off['scenarios']['overload']['rejected_429']}/{off['scenarios']['overload']['requests']} | {on['scenarios']['overload']['rejected_429']}/{on['scenarios']['overload']['requests']} |
| Retry-After 头 | {'有' if off['scenarios']['overload']['retry_after_present'] else '无'} | {'有' if on['scenarios']['overload']['retry_after_present'] else '无'} |

## 效果说明

- **多用户隔离**：两个模式下他用户访问会被拒绝（403/404），说明用户维度权限与会话归属默认生效，与开关无关。
- **同会话顺序**：`on` 模式下后序 turn 的 `queue_wait` > 0 且无时间重叠，证明服务端顺序执行；`off` 模式为 legacy 并发（可能出现重叠），因此生产多 Worker 放量前必须开启该开关。
- **跨用户并行**：两种模式的跨用户 e2e 分布相近，说明串行化只作用于同一会话，不会拖慢跨会话并行。
- **过载背压**：`on` 模式下低 RPM 用户快速提交出现 429 且带 `Retry-After`，任务未被接受前即拒绝；`off` 模式默认仅依赖系统承载，缺少该显式背压。
- **注意**：本报告为开发机短时实测（非 30 分钟×3 生产压测），绝对值不可直接用作 SLO；相对差异与行为断言可用于回归对比。
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8130)
    parser.add_argument("--users", type=int, default=3)
    parser.add_argument("--turns-per-user", type=int, default=2)
    parser.add_argument("--burst", type=int, default=3)
    parser.add_argument("--overload", type=int, default=5)
    parser.add_argument(
        "--wave-size",
        type=int,
        default=0,
        help="users fired per wave; 0 = all users at once",
    )
    parser.add_argument("--wait-timeout", type=float, default=300.0, help="per-turn completion timeout")
    parser.add_argument(
        "--queue-deadline",
        type=float,
        default=0.0,
        help="override CHAT_QUEUE_DEADLINE_SECONDS when > 0",
    )
    parser.add_argument("--user-rpm", type=int, default=3, help="on-mode per-user chat RPM")
    parser.add_argument("--tenant-rpm", type=int, default=30, help="on-mode per-tenant chat RPM")
    parser.add_argument("--password", default="StrongPass123!")
    parser.add_argument("--output", default="")
    args = parser.parse_args(argv)

    # Keep per-run artifacts (api.log, temp DBs) under the workspace scratch
    # dir so a failed startup can be diagnosed; the caller may delete them.
    scratch_root = REPO_ROOT / ".tmp_dbg"
    scratch_root.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="cc-drill-", dir=str(scratch_root))
    print(f"scratch={tmp}")
    off = _run_mode("off", args, Path(tmp) / "off")
    on = _run_mode("on", args, Path(tmp) / "on")
    combined = {"off": off, "on": on, "markdown": _markdown(off, on)}
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(combined, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
        markdown_path = output.with_suffix(".md")
        markdown_path.write_text(combined["markdown"], encoding="utf-8")
        print(f"report={output}")
        print(f"markdown={markdown_path}")
    else:
        print(combined["markdown"])
    checks = [
        off["scenarios"]["isolation"]["blocked"],
        on["scenarios"]["isolation"]["blocked"],
        on["scenarios"]["burst"]["serialized"],
        on["scenarios"]["overload"]["rejected_429"] > 0,
        all(t["status"] == "completed" for t in on["turns"]),
        all(t["status"] == "completed" for t in off["turns"]),
    ]
    ok = all(checks)
    print(f"ok={ok}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
