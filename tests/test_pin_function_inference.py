from src.circuit.models import ComponentInstance, Pin
from src.document_authoring.pin_function_inference import (
    infer_pin_function_from_net,
    resolve_pin_function,
    select_connected_connector_pins,
)


def test_select_connected_connector_pins_keeps_only_x_and_j_with_real_networks():
    connectors = [
        ComponentInstance(refdes="X1900", library_cell="connector", pins=[
            Pin(name="&1", net="CAN0H"),
            Pin(name="&2", net=None),
            Pin(name="&3", net="None"),
        ]),
        ComponentInstance(refdes="J7", library_cell="connector", pins=[
            Pin(name="&1", net="VCC3V3"),
        ]),
        ComponentInstance(refdes="DP1600", library_cell="connector", pins=[
            Pin(name="&1", net="UBD"),
        ]),
    ]

    assert select_connected_connector_pins(connectors) == [
        {"refdes": "X1900", "pin_name": "1", "net_name": "CAN0H", "part_number": ""},
        {"refdes": "J7", "pin_name": "1", "net_name": "VCC3V3", "part_number": ""},
    ]


def test_manual_pin_description_wins_over_network_rule():
    result = resolve_pin_function(
        refdes="X1900",
        pin_name="1",
        net_name="CAN0H",
        evidence=[{
            "id": "manual-1",
            "content": "X1900 pin 1 (CAN0H): CAN high differential bus signal.",
            "metadata": {"source_role": "datasheet"},
        }],
    )

    assert result.function == "CAN high differential bus signal."
    assert result.source == "datasheet"
    assert result.evidence_ids == ["manual-1"]


def test_rule_fallback_is_explicit_and_unknown_network_is_not_invented():
    inferred = infer_pin_function_from_net("MIPI0_DATA0_P")
    assert inferred == "MIPI 摄像头高速差分数据信号（规则推断）"
    assert infer_pin_function_from_net("SOME_CUSTOM_NET") is None
