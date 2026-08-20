from src.circuit.models import ComponentInstance, Pin
from src.document_authoring.icd_generation import build_connector_rows


def test_build_connector_rows_keeps_connected_unconnected_and_ground_pins():
    connectors = [
        ComponentInstance(
            refdes="J7",
            library_cell="connector",
            part_number="PN-7",
            pins=[
                Pin(name="&1", net="CAN_H"),
                Pin(name="&2", net=None),
                Pin(name="&3", net="PGND"),
            ],
        ),
    ]

    rows = build_connector_rows(connectors, function_notes={"j7:1": "CAN 通讯"})

    assert rows == [
        {
            "pin": "J7-1",
            "definition": "CAN_H",
            "function": "CAN 通讯",
            "notice": "",
        },
        {
            "pin": "J7-2",
            "definition": "NC",
            "function": "",
            "notice": "源文件未声明网络连接",
        },
        {
            "pin": "J7-3",
            "definition": "PGND",
            "function": "地/回流连接（规则推断）",
            "notice": "",
        },
    ]


def test_build_connector_rows_resolves_xj_functions_from_manual_then_rules():
    connectors = [
        ComponentInstance(
            refdes="X1900",
            library_cell="connector",
            part_number="CONN-1900",
            pins=[Pin(name="&1", net="CAN0H"), Pin(name="&2", net=None)],
        ),
        ComponentInstance(
            refdes="J7",
            library_cell="connector",
            pins=[Pin(name="&1", net="VCC3V3")],
        ),
    ]

    rows = build_connector_rows(
        connectors,
        manual_evidence=[{
            "id": "manual-x1900-1",
            "content": "X1900 pin 1: CAN high differential bus signal.",
            "metadata": {"source_role": "datasheet"},
        }],
    )

    assert rows[0]["function"] == "CAN high differential bus signal."
    assert rows[1]["function"] == ""
    assert rows[2]["function"] == "电源供电连接（规则推断）"
