import unittest

from src.pipelines.document_rag.schemas import parse_status_view


class ParseStatusViewTests(unittest.TestCase):
    def test_failed_task_can_be_removed(self):
        view = parse_status_view("failed")

        self.assertTrue(view.is_failed)
        self.assertTrue(view.can_cancel)

    def test_completed_task_cannot_be_removed_from_task_panel(self):
        view = parse_status_view("parsed")

        self.assertTrue(view.is_success)
        self.assertFalse(view.can_cancel)

    def test_active_tasks_remain_cancellable(self):
        self.assertTrue(parse_status_view("queued").can_cancel)
        self.assertTrue(parse_status_view("parsing").can_cancel)


if __name__ == "__main__":
    unittest.main()
