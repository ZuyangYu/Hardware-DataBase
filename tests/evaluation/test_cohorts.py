import unittest

from src.evaluation.cohorts import evaluation_cohort
from src.evaluation.schemas import EvaluationSample


def _sample(tags: list[str]) -> EvaluationSample:
    return EvaluationSample(
        id="q1",
        question="Q",
        reference_answer="A",
        kb_name="ADAS",
        tags=tags,
    )


class EvaluationCohortTests(unittest.TestCase):
    def test_permission_and_direct_samples_are_non_retrieval(self):
        self.assertEqual(evaluation_cohort(_sample(["permission", "isolation"])), "non_retrieval")
        self.assertEqual(evaluation_cohort(_sample(["direct", "small-talk"])), "non_retrieval")

    def test_knowledge_base_question_is_retrieval(self):
        self.assertEqual(evaluation_cohort(_sample(["circuit", "pins"])), "retrieval")


if __name__ == "__main__":
    unittest.main()
