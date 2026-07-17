from __future__ import annotations

from src.agents.claim_evidence import EvidenceCapability
from src.circuit.ingest_workers import parse_edf_netlist, parse_schematic_pdf
from src.ingestion.parser_registry import DomainManifest, PARSER_REGISTRY
from src.ingestion.source_groups import NETLIST_GROUP, SCHEMATIC_GROUP


CIRCUIT_MANIFEST = DomainManifest(
    name="circuit",
    source_groups=(NETLIST_GROUP, SCHEMATIC_GROUP),
    parser_factories={
        NETLIST_GROUP: parse_edf_netlist,
        SCHEMATIC_GROUP: parse_schematic_pdf,
    },
    capabilities={
        NETLIST_GROUP: (
            EvidenceCapability(
                name="entity_lookup",
                content_kinds=["circuit_design"],
                direct_fact=True,
            ),
            EvidenceCapability(
                name="relationship_lookup",
                content_kinds=["circuit_design"],
                direct_fact=True,
            ),
        ),
        SCHEMATIC_GROUP: (
            EvidenceCapability(
                name="entity_lookup",
                content_kinds=["circuit_design"],
                direct_fact=True,
            ),
        ),
    },
)


def register():
    PARSER_REGISTRY.register_manifest(CIRCUIT_MANIFEST)
