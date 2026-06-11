import json
import os
import shutil
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Callable

import config.settings
from src.core.logger import error, log


TASK_STATUS_QUEUED = "queued"
TASK_STATUS_RUNNING = "running"
TASK_STATUS_PAUSED = "paused"
TASK_STATUS_COMPLETED = "completed"
TASK_STATUS_FAILED = "failed"
TASK_STATUS_CANCELLED = "cancelled"

TERMINAL_STATUSES = {TASK_STATUS_COMPLETED, TASK_STATUS_FAILED, TASK_STATUS_CANCELLED}


class ParseTaskCancelled(Exception):
    pass


@dataclass
class ParseTask:
    id: str
    kb_name: str
    source_path: str
    original_name: str
    source_group: str
    created_by: str = ""
    status: str = TASK_STATUS_QUEUED
    progress: int = 0
    stage: str = "等待解析"
    message: str = ""
    result: str = ""
    document_id: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None


class ParseTaskManager:
    """Small in-process parser task runner with JSON persistence for Streamlit."""

    def __init__(self, worker: Callable[[str, str, str, Callable[[int, str], None], Callable[[], None]], str]):
        self.worker = worker
        self.task_dir = os.path.join(config.settings.STORAGE_DIR, "parse_tasks")
        self.upload_dir = os.path.join(self.task_dir, "uploads")
        self.task_file = os.path.join(self.task_dir, "tasks.json")
        os.makedirs(self.upload_dir, exist_ok=True)
        self._lock = threading.RLock()
        self._threads: dict[str, threading.Thread] = {}
        self._cancel_events: dict[str, threading.Event] = {}
        self._tasks: dict[str, ParseTask] = self._load_tasks()
        self._recover_interrupted_tasks()

    def _load_tasks(self) -> dict[str, ParseTask]:
        if not os.path.exists(self.task_file):
            return {}
        try:
            with open(self.task_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            return {item["id"]: ParseTask(**item) for item in data}
        except Exception as exc:
            error(f"解析任务状态读取失败: {exc}")
            return {}

    def _save_tasks(self):
        os.makedirs(self.task_dir, exist_ok=True)
        data = [asdict(task) for task in sorted(self._tasks.values(), key=lambda t: t.created_at, reverse=True)]
        tmp_path = f"{self.task_file}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, self.task_file)

    def _recover_interrupted_tasks(self):
        changed = False
        for task in self._tasks.values():
            if task.status in {TASK_STATUS_RUNNING, TASK_STATUS_PAUSED, TASK_STATUS_QUEUED}:
                task.status = TASK_STATUS_FAILED
                task.stage = "任务中断"
                task.message = "应用重启或进程中断，解析任务未完成"
                task.finished_at = time.time()
                task.updated_at = task.finished_at
                changed = True
        if changed:
            self._save_tasks()

    def submit(self, kb_name: str, file_paths: list[str], source_group: str, created_by: str = "") -> list[ParseTask]:
        tasks = []
        with self._lock:
            for file_path in file_paths:
                task_id = uuid.uuid4().hex
                original_name = os.path.basename(file_path)
                staged_name = f"{task_id}_{original_name}"
                staged_path = os.path.join(self.upload_dir, staged_name)
                shutil.copy2(file_path, staged_path)
                task = ParseTask(
                    id=task_id,
                    kb_name=kb_name,
                    source_path=staged_path,
                    original_name=original_name,
                    source_group=source_group,
                    created_by=created_by,
                )
                self._tasks[task_id] = task
                self._cancel_events[task_id] = threading.Event()
                tasks.append(task)
            self._save_tasks()
        for task in tasks:
            self._start_task(task.id)
        return tasks

    def list_tasks(self, kb_name: str | None = None) -> list[ParseTask]:
        with self._lock:
            tasks = list(self._tasks.values())
        if kb_name:
            tasks = [task for task in tasks if task.kb_name == kb_name]
        return sorted(tasks, key=lambda task: task.created_at, reverse=True)

    def get_task(self, task_id: str) -> ParseTask | None:
        with self._lock:
            return self._tasks.get(task_id)

    def pause(self, task_id: str) -> bool:
        with self._lock:
            task = self._tasks.get(task_id)
            if not task or task.status not in {TASK_STATUS_QUEUED, TASK_STATUS_RUNNING}:
                return False
            task.status = TASK_STATUS_PAUSED
            task.stage = "已暂停"
            task.updated_at = time.time()
            self._save_tasks()
            return True

    def resume(self, task_id: str) -> bool:
        with self._lock:
            task = self._tasks.get(task_id)
            if not task or task.status != TASK_STATUS_PAUSED:
                return False
            task.status = TASK_STATUS_QUEUED
            task.stage = "等待解析"
            task.updated_at = time.time()
            self._save_tasks()
        self._start_task(task_id)
        return True

    def delete(self, task_id: str) -> bool:
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return False
            self._cancel_events.setdefault(task_id, threading.Event()).set()
            if task.status == TASK_STATUS_RUNNING:
                task.stage = "取消中"
                task.message = "已请求取消，将在安全阶段停止；若已开始写入索引则会完成写入"
                task.updated_at = time.time()
                self._save_tasks()
                return True
            self._tasks.pop(task_id, None)
            self._cancel_events.pop(task_id, None)
            self._cleanup_source(task.source_path)
            self._save_tasks()
            return True

    def clear_finished(self, kb_name: str | None = None):
        with self._lock:
            removable = [
                task_id for task_id, task in self._tasks.items()
                if task.status in TERMINAL_STATUSES and (kb_name is None or task.kb_name == kb_name)
            ]
            for task_id in removable:
                task = self._tasks.pop(task_id)
                self._cancel_events.pop(task_id, None)
                self._cleanup_source(task.source_path)
            self._save_tasks()

    def _start_task(self, task_id: str):
        with self._lock:
            task = self._tasks.get(task_id)
            if not task or task.status != TASK_STATUS_QUEUED:
                return
            thread = self._threads.get(task_id)
            if thread and thread.is_alive():
                return
            self._cancel_events.setdefault(task_id, threading.Event())
            thread = threading.Thread(target=self._run_task, args=(task_id,), daemon=True)
            self._threads[task_id] = thread
            thread.start()

    def _run_task(self, task_id: str):
        def update(progress: int, stage: str):
            with self._lock:
                task = self._tasks.get(task_id)
                if not task or task.status == TASK_STATUS_CANCELLED:
                    return
                task.progress = max(task.progress, min(100, progress))
                task.stage = stage
                task.updated_at = time.time()
                self._save_tasks()

        def checkpoint():
            while True:
                with self._lock:
                    task = self._tasks.get(task_id)
                    status = task.status if task else TASK_STATUS_CANCELLED
                if self._cancel_events.get(task_id, threading.Event()).is_set() or status == TASK_STATUS_CANCELLED:
                    raise ParseTaskCancelled()
                if status != TASK_STATUS_PAUSED:
                    return
                time.sleep(0.5)

        with self._lock:
            task = self._tasks.get(task_id)
            if not task or task.status != TASK_STATUS_QUEUED:
                return
            task.status = TASK_STATUS_RUNNING
            task.started_at = task.started_at or time.time()
            task.updated_at = task.started_at
            self._save_tasks()

        try:
            checkpoint()
            result = self.worker(task.kb_name, task.source_path, task.source_group, update, checkpoint)
            with self._lock:
                task = self._tasks.get(task_id)
                if task and task.status != TASK_STATUS_CANCELLED:
                    task.status = TASK_STATUS_COMPLETED
                    task.progress = 100
                    task.stage = "解析完成"
                    task.result = result
                    task.document_id = self._extract_document_id(result) or task.original_name
                    task.message = result
                    task.finished_at = time.time()
                    task.updated_at = task.finished_at
                    self._save_tasks()
        except ParseTaskCancelled:
            with self._lock:
                task = self._tasks.get(task_id)
                if task:
                    task.status = TASK_STATUS_CANCELLED
                    task.stage = "已取消"
                    task.message = "解析任务已取消"
                    task.finished_at = time.time()
                    task.updated_at = task.finished_at
                    self._save_tasks()
        except Exception as exc:
            with self._lock:
                task = self._tasks.get(task_id)
                if task:
                    task.status = TASK_STATUS_FAILED
                    task.stage = "解析失败"
                    task.message = str(exc)
                    task.finished_at = time.time()
                    task.updated_at = task.finished_at
                    self._save_tasks()
            error(f"解析任务失败 {task_id}: {exc}")
        finally:
            with self._lock:
                task = self._tasks.get(task_id)
                if task and task.status in {TASK_STATUS_COMPLETED, TASK_STATUS_CANCELLED}:
                    self._cleanup_source(task.source_path)
                self._threads.pop(task_id, None)
                self._cancel_events.pop(task_id, None)

    def _cleanup_source(self, path: str):
        try:
            if path and os.path.exists(path):
                os.remove(path)
        except OSError as exc:
            log(f"解析任务临时文件清理失败: {path}, {exc}")

    def _extract_document_id(self, result: str) -> str:
        marker = "索引成功:"
        if marker in result:
            return result.split(marker, 1)[1].strip()
        return ""
