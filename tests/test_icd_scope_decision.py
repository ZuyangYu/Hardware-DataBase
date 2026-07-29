from types import SimpleNamespace

from src.document_authoring.icd_scope_decision import build_icd_scope_decision


def evidence(
    source_name: str,
    content: str,
    *,
    document_role: str | None = None,
    source_group: str | None = None,
):
    metadata = {}
    if document_role is not None:
        metadata["document_role"] = document_role
    if source_group is not None:
        metadata["source_group"] = source_group
    return SimpleNamespace(source_name=source_name, content=content, metadata=metadata)


def pin_mapping(refdes: str, pins: list[tuple[str, str | None]]):
    return SimpleNamespace(
        source_name="board.edf",
        content="",
        locator={"entity_id": refdes},
        metadata={
            "pin_mappings": [
                {"refdes": refdes, "pin_name": pin_name, "net_name": net_name}
                for pin_name, net_name in pins
            ]
        },
    )


def test_direct_circuit_and_supporting_reference_are_auto_adopted():
    decision = build_icd_scope_decision(
        circuit_evidences=[pin_mapping("J7", [("1", "CAN_H")])],
        supporting_evidences=[evidence(
            "FPT", "J7-1 CAN communication", document_role="fpt",
        )],
    )

    assert decision.auto_items[0].refdes == "J7"
    assert decision.exceptions == []
    assert decision.frozen_pin_mappings == [
        {"refdes": "J7", "pin_name": "1", "net_name": "CAN_H", "source_name": "board.edf"}
    ]


def test_pgnd_without_direct_external_reference_requires_one_exception():
    decision = build_icd_scope_decision([pin_mapping("J7", [("3", "PGND")])], [])

    issue = decision.exceptions[0]
    assert issue.kind == "extra_pin_exposure"
    assert issue.recommended_action == "mark_pending"
    assert issue.user_instruction == "确认该脚是否需要在对外 ICD 中暴露。"


def test_unmapped_pin_is_frozen_as_nc():
    decision = build_icd_scope_decision(
        [pin_mapping("J7", [("2", None)])],
        [evidence("FPT", "J7-2 spare input", document_role="fpt")],
    )

    assert decision.frozen_pin_mappings == [
        {"refdes": "J7", "pin_name": "2", "net_name": "NC", "source_name": "board.edf"}
    ]


def test_reservation_wording_without_direct_pin_reference_is_an_exception():
    decision = build_icd_scope_decision(
        [pin_mapping("J7", [("2", "GPIO")])],
        [evidence(
            "requirements", "该接口预留，后续可能裁剪。",
            document_role="requirements",
        )],
    )

    assert {issue.kind for issue in decision.exceptions} == {
        "extra_pin_exposure",
        "unsupported_reservation",
    }


def test_direct_reference_without_a_declared_requirement_or_fpt_role_is_not_auto_adopted():
    decision = build_icd_scope_decision(
        [pin_mapping("J7", [("1", "CAN_H")])],
        [evidence("notes.xlsx", "J7-1 CAN communication")],
    )

    assert decision.auto_items == []
    assert [issue.kind for issue in decision.exceptions] == ["extra_pin_exposure"]


def test_requirement_or_fpt_filename_is_accepted_when_kb_metadata_has_no_role():
    decision = build_icd_scope_decision(
        [pin_mapping("J7", [("1", "CAN_H")])],
        [evidence("600608964_ADAS_产品硬件需求规格说明书.xlsx", "J7-1 CAN communication")],
    )

    assert [item.refdes for item in decision.auto_items] == ["J7"]


def test_direct_reference_from_design_or_legacy_icd_is_not_auto_adopted():
    for source in (
        evidence("board.pdf", "J7-1 CAN", document_role="schematic"),
        evidence("board.edf", "J7-1 CAN", document_role="netlist"),
        evidence("old-icd.xlsx", "J7-1 CAN", document_role="icd"),
    ):
        decision = build_icd_scope_decision(
            [pin_mapping("J7", [("1", "CAN_H")])], [source],
        )

        assert decision.auto_items == []
