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
            "function": "",
            "notice": "",
        },
    ]
