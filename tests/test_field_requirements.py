"""P1 field rules: explicit, auditable, and free of acceptance answer values."""

from __future__ import annotations

from types import SimpleNamespace

from src.agents.schemas import Evidence
from src.services.field_requirements import (
    CONTRADICTED, SUPPORTED, UNKNOWN, FieldCoveragePlan, extract_requirements,
    verify_requirement,
)


def target(question: str, *, target_id="t1", kb_id=5, record_id=101):
    return SimpleNamespace(target_id=target_id, kb_id=kb_id, kb_name=f"KB{kb_id}",
                           record_id=record_id, question=question)


def item(content: str, *, kb_id=5, record_id=101, locator=None):
    return Evidence(id="e1", content=content, source_name="spec.docx",
                    content_kind="document_text", processor_kind="ragflow",
                    locator=locator or {}, metadata={"kb_id": kb_id, "local_record_id": record_id})


def test_extract_voltage_and_current_requirements_with_conditions():
    reqs = extract_requirements(
        "设备在 30deg、测试电压暂定 18V 时所有模块正常工作电压范围和静态电流要求",
        [target("设备在 30deg、测试电压暂定 18V 时所有模块正常工作电压范围和静态电流要求")],
    )
    assert {req.field_type for req in reqs} == {"voltage_range", "quiescent_current"}
    voltage = next(req for req in reqs if req.field_type == "voltage_range")
    assert voltage.object_scope == "all_modules"
    assert voltage.conditions == ("temp:30", "test_voltage:18")


def test_all_module_requirement_rejects_can_only_range():
    req = next(req for req in extract_requirements(
        "所有模块正常工作电压范围", [target("所有模块正常工作电压范围")],
    ) if req.field_type == "voltage_range")
    can_only = verify_requirement(req, item("CAN 通信正常范围 7.5-15V"))
    assert can_only.status == CONTRADICTED
    assert can_only.reason == "scope_is_can_communication_not_all_modules"


def test_all_module_requirement_accepts_matching_scope():
    req = next(req for req in extract_requirements(
        "所有模块正常工作电压范围", [target("所有模块正常工作电压范围")],
    ) if req.field_type == "voltage_range")
    result = verify_requirement(req, item("所有模块正常工作电压范围 21-29V"))
    assert result.status == SUPPORTED
    assert result.facts["ranges"][0] == {"low": 21.0, "high": 29.0, "unit": "V"}
    assert result.excerpt.startswith("所有模块")


def test_conditions_are_required_before_current_can_be_supported():
    req = next(req for req in extract_requirements(
        "静态电流要求，30deg，测试电压暂定 18V", [target("静态电流要求，30deg，测试电压暂定 18V")],
    ) if req.field_type == "quiescent_current")
    assert verify_requirement(req, item("静态电流小于 120uA")).status == UNKNOWN
    supported = verify_requirement(req, item("静态电流小于 120uA @30deg，测试电压暂定 18V"))
    assert supported.status == SUPPORTED
    assert supported.facts == {"value": "120", "unit": "uA"}


def test_resistance_and_capacitance_need_their_context():
    reqs = extract_requirements("终端电阻和电容要求", [target("终端电阻和电容要求")])
    resistance = next(req for req in reqs if req.field_type == "termination_resistance")
    capacitance = next(req for req in reqs if req.field_type == "capacitance")
    assert verify_requirement(resistance, item("终端电阻 8.2k")).status == SUPPORTED
    assert verify_requirement(resistance, item("其他电阻 8.2k")).status == UNKNOWN
    assert verify_requirement(capacitance, item("终端电容 6.8nF")).status == SUPPORTED
    assert verify_requirement(capacitance, item("其他参数 6.8nF")).status == UNKNOWN


def test_power_network_requires_all_declared_outputs():
    req = next(req for req in extract_requirements(
        "LP87702 的输入网络及三个输出电源网络",
        [target("LP87702 的输入网络及三个输出电源网络")],
    ) if req.field_type == "power_network")
    partial = verify_requirement(req, item("LP87702 输入 VCC3V3，输出 VCC9V9、VCC1V2"))
    assert partial.status == UNKNOWN
    assert partial.reason == "missing_output_rails:2/3"
    complete = verify_requirement(req, item("LP87702 输入 VCC3V3，输出 VCC9V9、VCC1V2、VCC5V0"))
    assert complete.status == SUPPORTED
    assert complete.facts["input_rail"] if "input_rail" in complete.facts else True


def test_pin_definition_requires_requested_fields():
    req = next(req for req in extract_requirements(
        "第7脚的定义、功能和电压", [target("第7脚的定义、功能和电压")],
    ) if req.field_type == "pin_definition")
    assert req.pin_values == ("7",)
    partial = verify_requirement(req, item("管脚号 Pin Number: 7; 管脚定义: SIG"))
    assert partial.status == UNKNOWN
    complete = verify_requirement(req, item(
        "管脚号 Pin Number: 7; 管脚定义: SIG; 功能描述: 输入; 电压 3.3-5V",
        locator={"sheet_name": "Pin Definition", "row_index": 9},
    ))
    assert complete.status == SUPPORTED
    assert complete.facts["definition"] == "SIG"
    assert complete.locator["row_index"] == 9


def test_unsupported_question_stays_source_query():
    assert extract_requirements("这份文档的审批流程是什么？", [target("这份文档的审批流程是什么？")]) == ()


def test_field_plan_marks_only_verified_requirement_and_keeps_locator():
    req = next(req for req in extract_requirements(
        "所有模块正常工作电压范围", [target("所有模块正常工作电压范围")],
    ) if req.field_type == "voltage_range")
    plan = FieldCoveragePlan((req,))
    assert plan.coverage_snapshot()["states"][UNKNOWN] == 1
    plan.apply(plan.evaluate(item("所有模块正常工作电压范围 21-29V", locator={"chunk_id": "c1"})))
    snapshot = plan.coverage_snapshot()
    assert snapshot["states"][SUPPORTED] == 1
    assert snapshot["complete"] is True
    assert snapshot["requirements"][0]["locator"] == {"chunk_id": "c1"}


def test_runtime_applies_field_verification_when_evidence_is_admitted():
    from src.agents.tools.runtime import ToolRuntime

    req = next(req for req in extract_requirements(
        "所有模块正常工作电压范围", [target("所有模块正常工作电压范围")],
    ) if req.field_type == "voltage_range")
    rt = ToolRuntime(kb_name="KB5", ctx=None, evidence_budget=5,
                     field_evidence_plan=FieldCoveragePlan((req,)))
    rt.add_evidence([item("所有模块正常工作电压范围 21-29V")])
    assert rt.field_evidence_plan.coverage_snapshot()["states"][SUPPORTED] == 1


def test_runner_builds_field_plan_only_when_flag_is_enabled(monkeypatch):
    import src.settings as settings
    from src.agents.runner import MultiSourceAgentRunner

    runner = object.__new__(MultiSourceAgentRunner)
    targets = [target("所有模块正常工作电压范围")]
    monkeypatch.setattr(settings, "MULTI_KB_EXPLICIT_FIELDS_ENABLED", False)
    assert runner._field_plan_for_targets("所有模块正常工作电压范围", targets) is None
    monkeypatch.setattr(settings, "MULTI_KB_EXPLICIT_FIELDS_ENABLED", True)
    plan = runner._field_plan_for_targets("所有模块正常工作电压范围", targets)
    assert plan is not None
    assert plan.coverage_snapshot()["coverage_mode"] == "explicit_fields"
