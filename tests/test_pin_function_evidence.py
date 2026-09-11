"""Tests for evidence-grounded connector pin function extraction."""

from __future__ import annotations

from src.document_authoring.pin_function_evidence import (
    compose_function_evidence_text,
    extract_pin_function_candidates,
    extract_spreadsheet_pin_function_candidates,
    pin_function_queries,
)


def _mapping(refdes: str, pin: str, net: str) -> dict[str, str]:
    return {"refdes": refdes, "pin_name": pin, "net_name": net}


def test_function_queries_are_connector_scoped():
    assert pin_function_queries("X1900") == ["X1900 管脚 定义 功能 描述"]
    assert pin_function_queries("") == []


def test_html_table_row_is_extracted_by_function_column():
    content = (
        "<table><caption>X1900 主接插件引脚定义</caption>"
        "<tr><td>管脚Pin</td><td>功能 Function</td><td>备注Remark</td></tr>"
        "<tr><td>1</td><td>唤醒控制输入</td><td>常电唤醒</td></tr>"
        "<tr><td>2</td><td>车身CAN0高</td><td>诊断CAN</td></tr>"
        "</table>"
    )

    found = extract_pin_function_candidates(
        content,
        [_mapping("X1900", "1", "I_S_WKUP"), _mapping("X1900", "2", "CAN0H")],
    )

    assert found == {"X1900-1": "唤醒控制输入", "X1900-2": "车身CAN0高"}


def test_wrong_product_table_net_match_is_rejected():
    content = (
        "<table><caption>主接插件引脚定义</caption>"
        "<tr><td>管脚Pin</td><td>功能 Function</td></tr>"
        "<tr><td>2</td><td>CAN0H</td></tr>"
        "</table>"
    )

    found = extract_pin_function_candidates(
        content, [_mapping("X1900", "2", "CAN0H")],
    )

    assert found == {}


def test_pin_identity_row_without_refdes_requires_connector_in_content():
    content = (
        "<table><caption>X2000 connector pinout</caption>"
        "<tr><td>Pin</td><td>Function</td></tr>"
        "<tr><td>7</td><td>Power supply input</td></tr>"
        "</table>"
    )

    found = extract_pin_function_candidates(
        content, [_mapping("X2000", "7", "VCC_12V")],
    )

    assert found == {"X2000-7": "Power supply input"}


def test_plain_text_sentence_is_extracted_only_with_refdes_context():
    content = (
        "X1903-16 定义为 MIPI0_DATA0_P，用于摄像头高速差分数据输出；"
        "其他连接器也使用 MIPI0_DATA0_P 但与本项目无关。"
    )

    found = extract_pin_function_candidates(
        content, [_mapping("X1903", "16", "MIPI0_DATA0_P")],
    )

    assert "X1903-16" in found
    assert "MIPI0_DATA0_P" in found["X1903-16"]

    unrelated = extract_pin_function_candidates(
        content, [_mapping("X1999", "16", "MIPI0_DATA0_P")],
    )
    assert unrelated == {}


def test_candidate_is_bounded_and_excludes_identity_tokens():
    long_text = "功能描述" + "长" * 300
    content = (
        "<table><caption>X1900 引脚</caption>"
        "<tr><td>Pin</td><td>Function</td></tr>"
        f"<tr><td>3</td><td>{long_text}</td></tr>"
        "</table>"
    )

    found = extract_pin_function_candidates(
        content, [_mapping("X1900", "3", "CAN0L")],
    )

    assert "X1900-3" in found
    assert len(found["X1900-3"]) <= 120


def test_spreadsheet_row_uses_load_cell_and_strips_test_config():
    values = {
        "负载": "CAN0（终端电阻60.4R+60.4R）",
        "L2": "X1900-2",
        "L3": "X1900-3",
        "L4": "控制器接入CAN通讯，2Mbit/s",
        "L11": "评价准则",
    }

    found = extract_spreadsheet_pin_function_candidates(
        values,
        "STB,STG,OPL | X1900-2 | X1900-3 | 控制器接入CAN通讯，2Mbit/s",
        [_mapping("X1900", "2", "CAN0H"), _mapping("X1900", "3", "CAN0L")],
    )

    assert found == {"X1900-2": "CAN0", "X1900-3": "CAN0"}


def test_spreadsheet_row_requires_exact_pin_token():
    values = {"负载": "CAN0", "L2": "X1900-10", "L3": "X1900-11"}

    found = extract_spreadsheet_pin_function_candidates(
        values,
        "STB,STG,OPL | X1900-10 | X1900-11 | 控制器接入CAN通讯",
        [_mapping("X1900", "1", "I_S_WKUP")],
    )

    assert found == {}


def test_spreadsheet_row_falls_back_to_description_cell():
    values = {
        "负载": "NA",
        "L2": "X1900-7",
        "L3": "X1900-8",
        "测试目的": "控制器接入车载以太网通讯",
    }

    found = extract_spreadsheet_pin_function_candidates(
        values,
        "NA | X1900-7 | X1900-8 | 控制器接入车载以太网通讯",
        [_mapping("X1900", "7", "B_D_ETH_100_P"), _mapping("X1900", "8", "B_D_ETH_100_N")],
    )

    assert found == {
        "X1900-7": "控制器接入车载以太网通讯",
        "X1900-8": "控制器接入车载以太网通讯",
    }


def test_compose_function_evidence_contains_every_hit_and_source_row():
    text = compose_function_evidence_text({"X1900-2": "CAN0"}, "STB,STG,OPL | X1900-2")

    assert "X1900-2 CAN0" in text
    assert "STB,STG,OPL | X1900-2" in text
