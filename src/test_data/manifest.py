from __future__ import annotations

from src.ingestion.evidence_capability import EvidenceCapability
from src.ingestion.parser_registry import DomainManifest, PARSER_REGISTRY
from src.ingestion.source_groups import TEST_GROUP
from src.test_data.parsers import parse_test_data


TEST_DATA_MANIFEST = DomainManifest(
    name="test_data",
    source_groups=(TEST_GROUP,),
    parser_factories={
        TEST_GROUP: parse_test_data,
    },
    capabilities={
        TEST_GROUP: (
            EvidenceCapability(
                name="entity_lookup",
                content_kinds=["test_data"],
                direct_fact=True,
            ),
            EvidenceCapability(
                name="tabular_lookup",
                content_kinds=["test_data"],
                direct_fact=True,
            ),
        ),
    },
)


def register():
    PARSER_REGISTRY.register_manifest(TEST_DATA_MANIFEST)
