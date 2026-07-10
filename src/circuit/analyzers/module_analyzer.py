from __future__ import annotations

from collections import Counter

from src.circuit.models import CircuitModule, ComponentInstance, Net


class ModuleAnalyzer:
    def __init__(self, instances: list[ComponentInstance], nets: list[Net]):
        self.instances = {instance.refdes: instance for instance in instances}
        self.nets = {net.name: net for net in nets}

    def describe(self, module: CircuitModule) -> str:
        module_instances = [self.instances[ref] for ref in module.instances if ref in self.instances]
        module_nets = [self.nets[name] for name in module.nets if name in self.nets]
        cell_counts = Counter(instance.library_cell or "unknown" for instance in module_instances)
        net_counts = Counter(net.net_type for net in module_nets)
        top_cells = ", ".join(f"{name} x{count}" for name, count in cell_counts.most_common(8))
        power_nets = [net.name for net in module_nets if net.net_type in {"power", "ground"}][:12]
        clock_nets = [net.name for net in module_nets if net.net_type == "clock"][:8]
        signal_nets = [net.name for net in module_nets if net.net_type == "signal"][:12]
        parts = [
            f"{module.name} contains {len(module_instances)} instances and {len(module_nets)} related nets.",
        ]
        if top_cells:
            parts.append(f"Dominant library cells: {top_cells}.")
        if net_counts:
            parts.append(
                "Net classes: "
                + ", ".join(f"{name}={count}" for name, count in sorted(net_counts.items()))
                + "."
            )
        if power_nets:
            parts.append(f"Power/ground nets: {', '.join(power_nets)}.")
        if clock_nets:
            parts.append(f"Clock-like nets: {', '.join(clock_nets)}.")
        if signal_nets:
            parts.append(f"Representative signal nets: {', '.join(signal_nets)}.")
        return " ".join(parts)


def enrich_module_descriptions(
    modules: list[CircuitModule],
    instances: list[ComponentInstance],
    nets: list[Net],
) -> list[CircuitModule]:
    analyzer = ModuleAnalyzer(instances, nets)
    for module in modules:
        module.connectivity_description = analyzer.describe(module)
        module.merged_description = module.connectivity_description
    return modules
