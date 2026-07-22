from src.agents.answer_constraints import answer_contract, reportable_conflicts, response_scope


def test_key_question_scope_is_an_enumeration_of_key_facts():
    assert response_scope("U1700 的每个关键引脚连接到哪个网络？") == {
        "requested_detail": "key",
        "response_shape": "enumeration",
    }


def test_conflicts_require_same_entity_field_and_distinct_sources():
    noisy = {
        "source_name": "hsi.docx",
        "content": "register: nan, description: reset",
    }
    first = {
        "source_name": "source-a",
        "metadata": {"entity_id": "U1", "field": "output_voltage", "value": "1.0V"},
    }
    second = {
        "source_name": "source-b",
        "metadata": {"entity_id": "U1", "field": "output_voltage", "value": "1.2V"},
    }

    assert reportable_conflicts([noisy]) == []
    assert reportable_conflicts([first, second]) == [
        {
            "field": "u1.output_voltage",
            "values": [
                {"value": "1.0v", "sources": ["source-a"]},
                {"value": "1.2v", "sources": ["source-b"]},
            ],
            "reason": "不同来源对同一实体字段给出不同取值",
        }
    ]


def test_conflicts_infer_main_mcu_model_difference_from_circuit_and_document_evidence():
    conflicts = reportable_conflicts(
        [
            {
                "source_name": "board.edf",
                "content_kind": "circuit_design",
                "content": "Instance U900: SAK-TC367DP-64F300S.",
            },
            {
                "source_name": "hsi.docx",
                "content_kind": "document_text",
                "content": "32-bit MCU TC377 minimum system module.",
            },
        ],
        question="主控 MCU 是哪颗，型号是什么？",
    )

    assert conflicts == [
        {
            "field": "main_mcu.model",
            "values": [
                {"value": "tc367", "sources": ["board.edf"]},
                {"value": "tc377", "sources": ["hsi.docx"]},
            ],
            "reason": "不同来源对同一实体字段给出不同取值",
        }
    ]


def test_answer_contract_requires_scoped_grounded_output():
    contract = answer_contract(
        "U1700 的每个关键引脚连接到哪个网络？",
        [{"claim_id": "c1", "status": "supported"}],
        [{"sub_question_id": "sq2", "status": "missing"}],
        [],
    )

    assert "回答范围" in contract
    assert "只列出问题要求的关键项" in contract
    assert "没有直接证据时明确说明缺口" in contract
