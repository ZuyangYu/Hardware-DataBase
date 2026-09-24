"""P3-001/P3-002: reviewer and research subagents (flag gated)."""

from __future__ import annotations

import unittest

import src.settings
from src.agents.subagents import (
    ResearchAgent,
    ReviewerAgent,
    research_agent_enabled,
    reviewer_agent_enabled,
)
from src.agents.subagents.reviewer_agent import review_document


class _FailingModel:
    def invoke(self, *_args, **_kwargs):
        raise RuntimeError("model unavailable")


class ReviewerAgentTests(unittest.TestCase):
    def setUp(self):
        self._original = src.settings.REVIEWER_AGENT_ENABLED
        src.settings.REVIEWER_AGENT_ENABLED = True
        self.addCleanup(setattr, src.settings, "REVIEWER_AGENT_ENABLED", self._original)
        self.agent = ReviewerAgent(enabled=True)
        self.evidence = [{"id": "e1", "content": "VIN 最大 6V"}]

    def test_disabled_agent_refuses_and_convenience_returns_none(self):
        src.settings.REVIEWER_AGENT_ENABLED = False
        self.assertFalse(reviewer_agent_enabled())
        with self.assertRaises(RuntimeError):
            ReviewerAgent().review(document_text="x [1]", evidence=self.evidence)
        self.assertIsNone(review_document(document_text="x", evidence=[]))

    def test_clean_document_with_valid_citations_passes(self):
        result = self.agent.review(document_text="VIN 最大为 6V [1]。", evidence=self.evidence)
        self.assertEqual(result.status, "pass")
        self.assertEqual(result.reviewer_version, "reviewer-v1")
        self.assertEqual(result.issues, [])

    def test_unknown_citation_fails(self):
        result = self.agent.review(document_text="结论 [3]。", evidence=self.evidence)
        self.assertEqual(result.status, "fail")
        self.assertEqual(result.issues[0].code, "unknown_citation")

    def test_claims_without_evidence_fail(self):
        result = self.agent.review(document_text="该器件绝对最大值为 10V。", evidence=[])
        codes = {issue.code for issue in result.issues}
        self.assertIn("no_evidence", codes)
        self.assertEqual(result.status, "fail")

    def test_required_and_forbidden_facts(self):
        result = self.agent.review(
            document_text="器件支持 6V 输入 [1]，禁止高温运行。",
            evidence=self.evidence,
            required_facts=["隔离电压 5kV"],
            forbidden_claims=["禁止高温运行"],
        )
        codes = {issue.code for issue in result.issues}
        self.assertIn("missing_required_fact", codes)
        self.assertIn("forbidden_claim_present", codes)
        self.assertEqual(result.status, "fail")

    def test_semantic_pass_failure_degrades_to_warn(self):
        agent = ReviewerAgent(_FailingModel(), enabled=True)
        result = agent.review(document_text="结论 [1]。", evidence=self.evidence)
        self.assertEqual(result.status, "warn")
        self.assertTrue(result.recommendations)


class ResearchAgentTests(unittest.TestCase):
    def setUp(self):
        self._original = src.settings.RESEARCH_AGENT_ENABLED
        src.settings.RESEARCH_AGENT_ENABLED = True
        self.addCleanup(setattr, src.settings, "RESEARCH_AGENT_ENABLED", self._original)

    def test_disabled_agent_refuses(self):
        src.settings.RESEARCH_AGENT_ENABLED = False
        self.assertFalse(research_agent_enabled())
        with self.assertRaises(RuntimeError):
            ResearchAgent(enabled=False).research(goal="x")

    def test_scope_is_frozen_and_conflicts_reported(self):
        loaded = [
            {"id": "e1", "content": "A", "source_name": "s1"},
            {"id": "e2", "content": "B", "source_name": "s2"},
            {"id": "secret", "content": "not allowed", "source_name": "s3"},
        ]
        agent = ResearchAgent(evidence_loader=lambda _goal: loaded, enabled=True)
        result = agent.research(goal="比较来源", evidence_refs=["e1", "e2"])
        self.assertEqual(result.evidence_refs, ["e1", "e2"])
        self.assertEqual(len(result.findings), 2)
        self.assertNotIn("secret", result.evidence_refs)

    def test_conflicts_and_missing_information(self):
        loaded = [{"id": "e1", "content": "冲突", "conflicts_with": "e2"}]
        agent = ResearchAgent(evidence_loader=lambda _goal: loaded, enabled=True)
        result = agent.research(goal="冲突检查", evidence_refs=["e1"])
        self.assertEqual(result.conflicts, ["e2"])
        self.assertEqual(result.depth, 1)
        empty = ResearchAgent(evidence_loader=lambda _goal: [], enabled=True)
        self.assertEqual(empty.research(goal="无证据").missing_information, ["no evidence matched the research scope"])


if __name__ == "__main__":
    unittest.main()
