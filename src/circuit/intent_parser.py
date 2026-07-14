from __future__ import annotations

import re

from src.circuit.query_context import CircuitIntent


_REFDES_RE = re.compile(r"\b([A-Za-z]{1,4}\d+[A-Za-z0-9_.-]*)\b")
_NET_RE = re.compile(
    r"(?<![A-Za-z0-9_./+-])("
    r"[+]?[A-Za-z0-9][A-Za-z0-9_./+-]*V[A-Za-z0-9_./+-]*"  # 5V, 3V3, +3.3V
    r"|VCC|VDD|VBAT|VSS|VREF|VCCA|VDDD|AVDD|AVCC|DVDD|DVCC"  # common supply rails
    r"|GND|VOUT|VIN|USB_[A-Za-z0-9_+-]+"
    r"|[A-Za-z]{2,}_[A-Za-z0-9_+-]+"  # VCC_5V, P5V (underscore-separated)
    r")(?![A-Za-z0-9_./+-])",
    re.IGNORECASE,
)
_ORDINAL_RE = re.compile(r"第\s*([一二三四五六七八九十\d]+)\s*个")


_GLOBAL_TERMS = ("所有", "全部", "整个知识库", "全部edf", "所有edf", "all circuits", "all edf")
_MODULE_LIST_TERMS = (
    "有多少个模块",
    "多少个模块",
    "各模块",
    "模块名称",
    "有哪些模块",
    "模块列表",
    "分几块",
    "功能单元",
    "组成部分",
    "由哪些部分组成",
    "list modules",
)
_OVERVIEW_TERMS = ("电路概况", "设计概况", "整体结构", "overview", "summary")
_CONNECTION_TERMS = ("连到", "连接", "接到", "接了", "哪些网络", "哪些引脚", "connections", "connection")
_MODULE_DETAIL_TERMS = ("有哪些元件", "有哪些器件", "包含哪些", "模块详情", "模块内", "模块里", "在哪里", "在哪", "位置")
_POWER_TERMS = ("电源", "供电", "电压", "电源轨", "power", "supply", "rail", "vcc", "vdd", "vbat", "pwr")
_POWER_QUERY_TERMS = (
    "电源树",
    "电源拓扑",
    "电源域",
    "供电树",
    "供电拓扑",
    "拓扑图",
    "接了哪些电压",
    "接哪些电压",
    "输入电压",
    "输入电压值",
    "供电电压",
    "工作电压",
    "电压值",
    "电源电压",
    "供电网络",
    "电源网络",
    "电源分配",
    "电源经过",
    "供电经过",
    "电源轨",
    "input voltage",
    "supply voltage",
    "operating voltage",
    "voltage value",
    "power rails",
    "power tree",
    "power topology",
    "power domain",
    "power distribution",
    "supply nets",
)
_POWER_TREE_TERMS = ("电源树", "电源拓扑", "供电树", "供电拓扑", "power tree", "power topology")
_TOPOLOGY_OUTPUT_TERMS = ("拓扑图", "关系图", "画出", "绘制", "mermaid", "diagram", "topology")
_POWER_DOMAIN_TERMS = ("电源域", "供电域", "power domain", "power rail", "rail domain")
_POWER_MODULE_SEARCH_TERMS = ("哪些电源模块", "有哪些电源模块", "电源部分有哪些模块", "电源相关模块", "供电模块", "power modules", "supply modules")
_FOLLOWUP_MODULE_TERMS = ("它", "这个模块", "该模块", "刚才那个", "刚才的", "上一个")
_NET_CONNECTION_TERMS = ("网络连接", "连了哪些器件", "连接了哪些器件", "连接哪些元件", "哪些器件连到", "哪些元件连到", "有哪些连接", "接到哪里", "连到哪里", "net connections", "connected components")
_ENTITY_SEARCH_TERMS = ("哪个edf", "哪些edf", "哪个电路", "哪些电路", "包含", "存在")
# Phrasing that marks a cross-circuit discovery question ("哪个 EDF 包含 X").
# ``包含`` is the strong signal; the others pair with an extracted entity.
_ENTITY_SEARCH_PHRASES = ("哪个edf", "哪些edf", "哪个电路", "哪些电路")

# ── Phase B/E additions (plan §3.4 standard intents) ──────────────────────
_CIRCUIT_LIST_TERMS = ("有哪些电路", "有哪些edf", "有哪些原理图", "多少个电路", "多少个edf", "哪些电路文件", "list circuits", "list edf")
_XREF_TERMS = ("交叉引用", "交叉对照", "edf和pdf", "edf 与 pdf", "edf与pdf", "对应关系", "映射状态", "对照状态", "cross reference", "cross-reference", "refdes映射", "refdes 映射")
_MODULE_INTERFACE_TERMS = ("接口", "对外", "external", "interfaces")
_RELATED_MODULE_TERMS = ("相连的模块", "连接的模块", "哪些模块和", "哪些模块连接到", "哪些模块连到", "哪些模块与", "connected modules", "related modules")
_PDF_LOCATION_TERMS = ("在哪一页", "哪一页", "第几页", "原理图位置", "原理图哪里", "在图上", "schematic location", "pdf位置", "pdf 位置", "page number")
_INSTANCE_DETAIL_TERMS = ("的详情", "的参数", "的封装", "的引脚", "元件详情", "器件详情", "instance detail")


def _norm(text: str) -> str:
    return (text or "").strip().lower().replace(" ", "")


def _ordinal_value(text: str) -> int | None:
    match = _ORDINAL_RE.search(text or "")
    if not match:
        return None
    raw = match.group(1)
    if raw.isdigit():
        return int(raw)
    mapping = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
    if raw in mapping:
        return mapping[raw]
    if raw.startswith("十") and len(raw) == 2:
        return 10 + mapping.get(raw[1], 0)
    if raw.endswith("十") and len(raw) == 2:
        return mapping.get(raw[0], 0) * 10
    if "十" in raw and len(raw) == 3:
        return mapping.get(raw[0], 0) * 10 + mapping.get(raw[2], 0)
    return None


class IntentParser:
    """Deterministic P0 circuit intent parser."""

    def parse(self, question: str) -> CircuitIntent:
        text = question or ""
        lowered = _norm(text)
        is_global = any(term in lowered for term in _GLOBAL_TERMS)
        ordinal = _ordinal_value(text)

        # Cross-circuit discovery ("哪个 EDF 包含 CAN 接口"). Checked before
        # module_interfaces ("接口") and list_circuits so the entity is not
        # swallowed by a single-circuit intent. Plain "有哪些电路" (no entity)
        # extracts nothing and falls through to list_circuits.
        search_entity = self._extract_search_entity(text)
        if search_entity and ("包含" in lowered or any(phrase in lowered for phrase in _ENTITY_SEARCH_PHRASES)):
            return CircuitIntent(
                intent="entity_search",
                target_entity_type=None,
                entity_text=search_entity,
                required_fields=["circuits", "entities"],
                is_global_query=True,
                pre_intent="entity_search",
                confidence=0.8,
            )

        if any(term in lowered for term in _POWER_MODULE_SEARCH_TERMS):
            return CircuitIntent(
                intent="power_distribution",
                target_entity_type="circuit",
                required_fields=["power_nets", "ground_nets"],
                is_global_query=is_global,
                pre_intent="global_summary" if is_global else "single_circuit_query",
                confidence=0.82,
            )

        if any(term in lowered for term in _CIRCUIT_LIST_TERMS):
            return CircuitIntent(
                intent="list_circuits",
                target_entity_type="circuit",
                required_fields=["circuit_count", "circuit_names"],
                is_global_query=True,
                pre_intent="global_summary",
                confidence=0.9,
            )

        if any(term in lowered for term in _XREF_TERMS):
            return CircuitIntent(
                intent="cross_reference_status",
                target_entity_type="circuit",
                required_fields=["coverage", "mapped_count"],
                is_single_entity_detail=False,
                pre_intent="single_circuit_query",
                confidence=0.8,
            )

        # Whole-circuit / rail-scoped power distribution must be recognized
        # before generic "有哪些模块"; otherwise "1V8电源域下有哪些模块" is
        # misrouted to a full module listing.
        if any(term in lowered for term in _POWER_TREE_TERMS) or (
            any(term in lowered for term in _TOPOLOGY_OUTPUT_TERMS)
            and any(term in lowered for term in _POWER_TERMS)
        ):
            return CircuitIntent(
                intent="power_topology",
                target_entity_type="circuit",
                entity_text=self._extract_net(text),
                required_fields=["power_converters", "input_nets", "output_nets", "conversion_edges"],
                is_global_query=is_global,
                is_single_entity_detail=not is_global,
                pre_intent="global_summary" if is_global else "single_circuit_query",
                confidence=0.9,
            )

        if any(term in lowered for term in _POWER_DOMAIN_TERMS):
            return CircuitIntent(
                intent="power_distribution",
                target_entity_type="circuit",
                entity_text=self._extract_net(text),
                required_fields=["power_tree", "power_nets", "modules"],
                is_global_query=is_global,
                is_single_entity_detail=not is_global,
                pre_intent="global_summary" if is_global else "single_circuit_query",
                confidence=0.88,
            )

        if any(term in lowered for term in _MODULE_LIST_TERMS):
            return CircuitIntent(
                intent="list_modules",
                target_entity_type="circuit",
                required_fields=["module_count", "module_names"],
                is_global_query=is_global,
                pre_intent="global_summary" if is_global else "single_circuit_query",
                confidence=0.95,
            )

        if any(term in lowered for term in _OVERVIEW_TERMS):
            return CircuitIntent(
                intent="circuit_overview",
                target_entity_type="circuit",
                required_fields=["summary"],
                is_global_query=is_global,
                pre_intent="global_summary" if is_global else "single_circuit_query",
                confidence=0.85,
            )

        # Signal-path tracing ("信号从 U1 到 U3 经过哪些模块").
        endpoints = self._extract_signal_endpoints(text)
        if endpoints[0] and endpoints[1]:
            return CircuitIntent(
                intent="trace_signal_path",
                from_entity=endpoints[0],
                to_entity=endpoints[1],
                required_fields=["path", "hops"],
                is_single_entity_detail=True,
                pre_intent="entity_detail",
                confidence=0.85,
            )

        if any(term in lowered for term in _MODULE_INTERFACE_TERMS):
            entity = self._extract_module_entity(text)
            return CircuitIntent(
                intent="module_interfaces",
                target_entity_type="module",
                entity_text=entity,
                required_fields=["interfaces"],
                is_single_entity_detail=True,
                pre_intent="single_circuit_query",
                confidence=0.8,
            )

        if "模块" in lowered and any(term in lowered for term in ("哪些网络", "连接了哪些网络", "接了哪些网络")):
            entity = self._extract_module_entity(text)
            return CircuitIntent(
                intent="module_interfaces",
                target_entity_type="module",
                entity_text=entity,
                required_fields=["interfaces"],
                is_single_entity_detail=True,
                pre_intent="single_circuit_query",
                confidence=0.78,
            )

        if any(term in lowered for term in _PDF_LOCATION_TERMS):
            entity = self._extract_module_entity(text)
            return CircuitIntent(
                intent="pdf_location",
                target_entity_type="module",
                entity_text=entity,
                required_fields=["regions", "page_number"],
                is_single_entity_detail=True,
                pre_intent="single_circuit_query",
                confidence=0.8,
            )

        # Net-connection queries must be matched before power_distribution:
        # "VCC 网络连接了哪些器件" hits both POWER (VCC) and CONNECTION (连接)
        # terms, but is a net query, not a power-distribution query.
        net_name = self._extract_net(text)
        if net_name and (any(term in lowered for term in _NET_CONNECTION_TERMS) or any(term in lowered for term in _CONNECTION_TERMS)):
            return CircuitIntent(
                intent="net_connections",
                target_entity_type="net",
                entity_text=net_name,
                required_fields=["connections"],
                is_global_query=is_global or any(term in lowered for term in _ENTITY_SEARCH_TERMS),
                is_single_entity_detail=not any(term in lowered for term in _ENTITY_SEARCH_TERMS),
                pre_intent="entity_search" if any(term in lowered for term in _ENTITY_SEARCH_TERMS) else "entity_detail",
                confidence=0.88,
            )

        if any(term in lowered for term in _POWER_QUERY_TERMS) or (
            any(term in lowered for term in _POWER_TERMS) and any(term in lowered for term in _CONNECTION_TERMS)
        ):
            # A named supply net ("5V 电源经过哪些模块") scopes the distribution
            # tree to that net. Extract the net first — _extract_module_entity
            # would otherwise grab the trailing "V" of "5V" as a fake module.
            net = self._extract_net(text)
            entity = self._extract_module_entity(text) if not net else None
            return CircuitIntent(
                intent="power_distribution",
                target_entity_type="module" if entity else "circuit",
                entity_text=entity or net,
                required_fields=["power_nets", "ground_nets"],
                is_global_query=is_global,
                is_single_entity_detail=not is_global,
                pre_intent="global_summary" if is_global else "single_circuit_query",
                confidence=0.82,
            )

        if any(term in lowered for term in _FOLLOWUP_MODULE_TERMS) and any(term in lowered for term in _MODULE_DETAIL_TERMS):
            return CircuitIntent(
                intent="module_detail",
                target_entity_type="module",
                required_fields=["instances"],
                is_single_entity_detail=True,
                pre_intent="single_circuit_query",
                confidence=0.78,
            )

        if any(term in lowered for term in _RELATED_MODULE_TERMS):
            entity = self._extract_related_subject(text)
            return CircuitIntent(
                intent="find_related_modules",
                target_entity_type="module",
                entity_text=entity,
                required_fields=["connected_modules"],
                is_single_entity_detail=True,
                pre_intent="single_circuit_query",
                confidence=0.78,
            )

        refdes = self._extract_refdes(text)
        if refdes and any(term in lowered for term in _CONNECTION_TERMS):
            return CircuitIntent(
                intent="instance_connections",
                target_entity_type="instance",
                entity_text=refdes,
                required_fields=["connections"],
                is_global_query=is_global or any(term in lowered for term in _ENTITY_SEARCH_TERMS),
                is_single_entity_detail=not any(term in lowered for term in _ENTITY_SEARCH_TERMS),
                pre_intent="entity_search" if any(term in lowered for term in _ENTITY_SEARCH_TERMS) else "entity_detail",
                confidence=0.95,
            )

        if refdes and any(term in lowered for term in _INSTANCE_DETAIL_TERMS):
            return CircuitIntent(
                intent="instance_detail",
                target_entity_type="instance",
                entity_text=refdes,
                required_fields=["instance_detail"],
                is_single_entity_detail=True,
                pre_intent="entity_detail",
                confidence=0.85,
            )

        if ordinal and any(term in lowered for term in _MODULE_DETAIL_TERMS):
            return CircuitIntent(
                intent="module_detail",
                target_entity_type="module",
                required_fields=["instances"],
                is_single_entity_detail=True,
                pre_intent="single_circuit_query",
                ordinal=ordinal,
                confidence=0.9,
            )

        if "模块" in lowered and any(term in lowered for term in _MODULE_DETAIL_TERMS):
            entity = self._extract_module_entity(text)
            return CircuitIntent(
                intent="module_detail",
                target_entity_type="module",
                entity_text=entity,
                required_fields=["instances"],
                is_single_entity_detail=True,
                pre_intent="single_circuit_query",
                confidence=0.75,
            )

        entity = self._extract_module_entity(text)
        if entity and any(term in lowered for term in _MODULE_DETAIL_TERMS):
            return CircuitIntent(
                intent="module_detail",
                target_entity_type="module",
                entity_text=entity,
                required_fields=["instances"],
                is_single_entity_detail=True,
                pre_intent="single_circuit_query",
                confidence=0.72,
            )

        return CircuitIntent(intent="unsupported_or_unclear", pre_intent="ambiguous", confidence=0.0)

    def pre_parse(self, question: str) -> str:
        return self.parse(question).pre_intent

    @staticmethod
    def _extract_refdes(text: str) -> str | None:
        match = _REFDES_RE.search(text or "")
        return match.group(1) if match else None

    @staticmethod
    def _extract_net(text: str) -> str | None:
        match = _NET_RE.search(text or "")
        return match.group(1) if match else None

    @staticmethod
    def _extract_module_entity(text: str) -> str | None:
        if not text:
            return None
        lowered = _norm(text)
        if any(term in lowered for term in _FOLLOWUP_MODULE_TERMS):
            return None
        match = re.search(r"([A-Za-z][A-Za-z0-9_.-]*(?:\s+[A-Za-z][A-Za-z0-9_.-]*){0,3})\s*(?:模块|部分|电路|单元)?", text)
        if match:
            value = match.group(1).strip()
            if value.lower() not in {"list", "power", "supply"}:
                return value
        match = re.search(r"([一-鿿]{2,12})\s*(?:模块|部分|电路|单元)", text)
        if match:
            value = match.group(1).strip()
            for suffix in ("模块", "部分", "电路", "单元"):
                if value.endswith(suffix):
                    value = value[: -len(suffix)]
            if value not in {"这个", "该", "各", "哪些", "多少个", "根据", "当前"}:
                return value
        return IntentParser._extract_before_module(text)

    @staticmethod
    def _extract_before_module(text: str) -> str | None:
        if not text:
            return None
        match = re.search(r"([A-Za-z0-9_.-]+|[一-鿿]+)\s*模块", text)
        if match:
            value = match.group(1).strip()
            if value not in {"这个", "该", "各", "哪些"}:
                return value
        return None

    @staticmethod
    def extract_source_files(text: str) -> list[str]:
        return re.findall(r"[A-Za-z0-9_.-]+\.(?:edf|edif|pdf)", text or "", flags=re.IGNORECASE)

    @staticmethod
    def _extract_signal_endpoints(text: str) -> tuple[str | None, str | None]:
        """Extract (from, to) entities from a signal-path question.

        Covers ``从 A 到 B``, ``from A to B`` and ``A 到 B 经过/路径``.
        """
        if not text:
            return None, None
        token = r"[A-Za-z0-9_./+-]+|[一-鿿]{1,12}"
        for pattern in (
            rf"从\s*({token})\s*到\s*({token})",
            r"from\s+([A-Za-z0-9_./+-]+)\s+to\s+([A-Za-z0-9_./+-]+)",
            r"([A-Za-z][A-Za-z0-9_]*)\s*到\s*([A-Za-z][A-Za-z0-9_]*)\s*(?:经过|的路径|路径|到达)",
        ):
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match:
                return match.group(1).strip(), match.group(2).strip()
        return None, None

    @staticmethod
    def _extract_related_subject(text: str) -> str | None:
        """Extract the module subject of a find-related-modules question."""
        if not text:
            return None
        token = r"[A-Za-z][A-Za-z0-9_.-]*|[一-鿿]{2,12}"
        for pattern in (
            rf"(?:和|与)\s*({token})\s*(?:相连|连接)",
            rf"({token})\s*(?:相连的模块|连接的模块)",
            rf"(?:连接到|连到|连接至)\s*({token})",
        ):
            match = re.search(pattern, text)
            if match:
                value = match.group(1).strip()
                if value not in {"哪些", "这个", "该", "各", "什么"}:
                    return value
        return None

    @staticmethod
    def _extract_search_entity(text: str) -> str | None:
        """Extract the searched entity from a cross-circuit discovery question.

        ``哪个 EDF 里包含 CAN 接口`` → ``CAN``; ``哪些电路有电源模块`` → ``电源``.
        Takes the substring after ``包含``/``有``, strips type suffixes
        (接口/模块/网络/…) and particles, and returns ``None`` for trivial
        leftovers so plain list questions fall through to list_circuits.
        """
        if not text:
            return None
        match = re.search(r"(?:包含|有)\s*(.+)$", text)
        if not match:
            return None
        raw = match.group(1).strip()
        raw = raw.rstrip("？?。.!！，, 、")
        for suffix in ("接口", "模块", "网络", "电路", "器件", "元件", "部分"):
            if raw.endswith(suffix):
                raw = raw[: -len(suffix)].strip()
        raw = raw.strip(" 里中的了、 ")
        if not raw or raw in {"哪些", "哪个", "多少个", "什么", "哪些电路", "哪些edf"}:
            return None
        return raw
