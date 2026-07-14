from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path

import config.settings
from src.ingestion.kb_paths import validate_kb_name
from src.test_data.models import TestReport


_ID_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def make_report_id(filename: str) -> str:
    stem = Path(filename).stem.strip() or "test_report"
    return _ID_RE.sub("_", stem)[:128]


class TestDataStore:
    def __init__(self, root: str | None = None):
        self.root = root or os.path.join(config.settings.STORAGE_DIR, "test_data")

    def report_dir(self, kb_name: str, report_id: str, create: bool = False) -> str:
        kb_name = validate_kb_name(kb_name)
        report_id = make_report_id(report_id)
        path = os.path.abspath(os.path.join(self.root, kb_name, report_id))
        root_abs = os.path.abspath(self.root)
        if os.path.commonpath([root_abs, path]) != root_abs:
            raise ValueError("Resolved test-data path escapes storage root.")
        if create:
            os.makedirs(path, exist_ok=True)
        return path

    def save(self, report: TestReport) -> str:
        path = self.report_dir(report.kb_name, report.report_id, create=True)
        target = os.path.join(path, "report.json")
        with open(target, "w", encoding="utf-8") as f:
            json.dump(report.to_dict(), f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        return target

    def load(self, kb_name: str, report_id: str) -> TestReport | None:
        target = os.path.join(self.report_dir(kb_name, report_id), "report.json")
        if not os.path.exists(target):
            return None
        with open(target, "r", encoding="utf-8") as f:
            return TestReport.from_dict(json.load(f))

    def delete_report(self, kb_name: str, report_id: str) -> bool:
        path = self.report_dir(kb_name, report_id)
        if not os.path.isdir(path):
            return False
        shutil.rmtree(path)
        return True

    def delete_kb(self, kb_name: str) -> bool:
        kb_dir = os.path.abspath(os.path.join(self.root, validate_kb_name(kb_name)))
        root_abs = os.path.abspath(self.root)
        if os.path.commonpath([root_abs, kb_dir]) != root_abs:
            raise ValueError("Resolved test-data KB path escapes storage root.")
        if not os.path.isdir(kb_dir):
            return False
        shutil.rmtree(kb_dir)
        return True

    def list_reports(self, kb_name: str) -> list[TestReport]:
        kb_dir = os.path.join(self.root, validate_kb_name(kb_name))
        if not os.path.isdir(kb_dir):
            return []
        reports = []
        for name in sorted(os.listdir(kb_dir)):
            loaded = self.load(kb_name, name)
            if loaded:
                reports.append(loaded)
        return reports
