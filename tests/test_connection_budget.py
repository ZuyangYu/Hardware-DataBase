"""Unit tests for the control-plane connection budget (v3 §9.5)."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from src.storage.control_plane.budget import (
    budget_fits,
    connection_budget,
    settings_connection_budget,
)


class ConnectionBudgetTests(unittest.TestCase):
    def test_budget_counts_every_process_class(self):
        budget = connection_budget(
            api_replicas=2,
            api_control_pool=8,
            api_checkpoint_pool=4,
            api_unpooled=4,
            worker_replicas=3,
            worker_control_pool=8,
            worker_checkpoint_pool=8,
            worker_unpooled=4,
            extra_processes={"memory": 1},
            reserve=20,
        )
        self.assertEqual(budget.entries["api"], 2 * 16)
        self.assertEqual(budget.entries["worker"], 3 * 20)
        self.assertEqual(budget.entries["memory"], 1)
        self.assertEqual(budget.total, 32 + 60 + 1 + 20)

    def test_headroom_check_rejects_oversubscription(self):
        budget = connection_budget(
            api_replicas=1,
            api_control_pool=10,
            api_checkpoint_pool=4,
            api_unpooled=0,
            worker_replicas=1,
            worker_control_pool=10,
            worker_checkpoint_pool=4,
            worker_unpooled=0,
        )
        fits, _ = budget_fits(budget, max_connections=100, minimum_headroom_ratio=0.3)
        self.assertTrue(fits)
        fits, message = budget_fits(budget, max_connections=30, minimum_headroom_ratio=0.3)
        self.assertFalse(fits)
        self.assertIn("headroom", message)

    def test_settings_budget_counts_authoring_and_memory_postgres(self):
        settings = SimpleNamespace(
            AGENT_CHECKPOINT_POOL_SIZE=4,
            CONTROL_PLANE_POOL_SIZE=8,
            DOCUMENT_AUTHORING_CHECKPOINTER_BACKEND="postgres",
            MEMORY_STORE_BACKEND="postgres",
        )
        budget = settings_connection_budget(settings, api_replicas=1, worker_replicas=1)
        # worker: 8 control + (4 agent + 4 authoring) checkpoint + 4 unpooled
        self.assertEqual(budget.entries["worker"], 20)
        self.assertEqual(budget.entries["memory"], 1)
        self.assertEqual(budget.entries["migration/ops"], 2)


if __name__ == "__main__":
    unittest.main()
