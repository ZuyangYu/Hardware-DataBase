"""Capacity reports must distinguish client shedding and accepted failures."""

import pytest

from scripts.production_hardening.mixed_load import distribution, summarize, validate_workload


def test_accepted_failure_denominator_excludes_rejection_and_client_shedding():
    report = summarize([
        {"accepted": True, "success": True, "e2e_s": 2, "ttft_s": 0.5},
        {"accepted": True, "success": False, "e2e_s": 20},
        {"submit_status": 429}, {"client_shed": True}, {"error_type": "ConnectError"},
    ])
    assert report["accepted_failure_rate"] == 0.5
    assert report["server_rejected"] == report["client_shed"] == report["transport_or_setup_failures"] == 1
    assert report["e2e_s"]["count"] == 2
    assert report["ttft_s"]["count"] == 1
    assert summarize([])["accepted_failure_rate"] is None
    assert distribution([])["p99"] is None


def test_document_mix_cannot_silently_run_without_real_template_payload():
    with pytest.raises(ValueError, match="document_payload"):
        validate_workload({"weights": {"chat": 1, "document": 1}})
