import unittest
from unittest.mock import patch

import config.settings
from src.pipelines.document_rag.ragflow_backend import RAGFlowBackend


class _DatasetStore:
    def __init__(self, mappings):
        self.mappings = dict(mappings)
        self.saved = []

    def get_dataset(self, kind):
        return self.mappings.get(kind)

    def get_dataset_id(self, kind):
        mapping = self.get_dataset(kind)
        return mapping[0] if mapping else None

    def save_dataset(self, kind, dataset_id, dataset_name):
        self.saved.append((kind, dataset_id, dataset_name))
        self.mappings[kind] = (dataset_id, dataset_name)


class _DatasetClient:
    def __init__(self):
        self.ensure_calls = []

    def ensure_dataset(self, name):
        self.ensure_calls.append(name)
        return f"id-{name}"


class _FailingDatasetClient(_DatasetClient):
    def ensure_dataset(self, name):
        self.ensure_calls.append(name)
        raise RuntimeError(f"cannot resolve {name}")


class RAGFlowDatasetMappingRefreshTests(unittest.TestCase):
    def _backend(self, mappings):
        backend = object.__new__(RAGFlowBackend)
        backend.store = _DatasetStore(mappings)
        backend.client = _DatasetClient()
        backend._dataset_ids = {}
        return backend

    @patch.object(config.settings, "RAGFLOW_GOVERNANCE_DATASET_NAME", "ADAS_new")
    @patch.object(config.settings, "RAGFLOW_DESIGN_DATASET_NAME", "ADAS_new")
    def test_refreshes_stale_persisted_mapping_when_dataset_name_changes(self):
        backend = self._backend({
            "governance": ("id-ADAS", "ADAS"),
            "design": ("id-ADAS", "ADAS"),
        })

        backend._ensure_physical_datasets()

        self.assertEqual(backend._dataset_ids, {
            "governance": "id-ADAS_new",
            "design": "id-ADAS_new",
        })
        self.assertEqual(backend.client.ensure_calls, ["ADAS_new", "ADAS_new"])
        self.assertEqual(backend.store.saved, [
            ("governance", "id-ADAS_new", "ADAS_new"),
            ("design", "id-ADAS_new", "ADAS_new"),
        ])

    @patch.object(config.settings, "RAGFLOW_GOVERNANCE_DATASET_NAME", "ADAS_new")
    @patch.object(config.settings, "RAGFLOW_DESIGN_DATASET_NAME", "ADAS_new")
    def test_reuses_matching_persisted_mapping_without_remote_resolution(self):
        backend = self._backend({
            "governance": ("id-ADAS_new", "ADAS_new"),
            "design": ("id-ADAS_new", "ADAS_new"),
        })

        backend._ensure_physical_datasets()

        self.assertEqual(backend._dataset_ids, {
            "governance": "id-ADAS_new",
            "design": "id-ADAS_new",
        })
        self.assertEqual(backend.client.ensure_calls, [])
        self.assertEqual(backend.store.saved, [])

    @patch.object(config.settings, "RAGFLOW_GOVERNANCE_DATASET_NAME", "ADAS_new")
    @patch.object(config.settings, "RAGFLOW_DESIGN_DATASET_NAME", "ADAS_new")
    def test_keeps_stale_mapping_when_replacement_cannot_be_resolved(self):
        backend = self._backend({
            "governance": ("id-ADAS", "ADAS"),
            "design": ("id-ADAS", "ADAS"),
        })
        backend.client = _FailingDatasetClient()

        with self.assertRaisesRegex(RuntimeError, "cannot resolve ADAS_new"):
            backend._ensure_physical_datasets()

        self.assertEqual(backend.store.mappings, {
            "governance": ("id-ADAS", "ADAS"),
            "design": ("id-ADAS", "ADAS"),
        })
        self.assertEqual(backend.store.saved, [])
