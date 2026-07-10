from __future__ import annotations

from src.circuit.models import CircuitModule


class MultimodalDescriptor:
    """Placeholder for Phase 4 visual enrichment.

    The visual description is additive only. Netlist-derived fields remain the
    source of truth and are never overwritten by a multimodal model.
    """

    def merge(self, module: CircuitModule, visual_description: str | None = None) -> CircuitModule:
        module.visual_description = visual_description
        if visual_description:
            module.merged_description = "\n".join(
                part for part in [module.connectivity_description, f"Visual notes: {visual_description}"] if part
            )
        else:
            module.merged_description = module.connectivity_description
        return module
