"""Module-tree browser for circuit designs.

Plan section 11.1 sketches a tree view: design → modules → instances.
This component is intentionally simple (Streamlit dataframe per level) but
keeps the rendering boundaries described in plan 4.3 — no imports from
`src/test_data/**` or `src/query_router/**`.
"""

from __future__ import annotations

import streamlit as st

from src.circuit.query_engine import CircuitQueryEngine


def render_module_tree(kb_name: str, key_prefix: str = "module_tree"):
    engine = CircuitQueryEngine()
    designs = engine.list_designs(kb_name)
    with st.container(border=True):
        st.markdown("##### 🧭 模块树浏览")
        if not designs:
            st.caption("当前知识库尚未解析 EDF/PDF，无可浏览的模块。")
            return

        design_id = st.selectbox(
            "设计",
            [d["design_id"] for d in designs],
            format_func=lambda did: _label(designs, did),
            key=f"{key_prefix}_design",
        )
        summary = engine.get_design_summary(kb_name, design_id)
        if not summary or not summary["modules"]:
            st.caption("该设计暂无模块划分结果。")
            return

        module_names = [module["name"] for module in summary["modules"]]
        selected = st.selectbox("模块", module_names, key=f"{key_prefix}_module")
        module = next(m for m in summary["modules"] if m["name"] == selected)

        c1, c2 = st.columns(2)
        c1.metric("实例数", module["instance_count"])
        c2.metric("网络数", module["net_count"])

        # Show instances belonging to this module by filtering search results.
        instances = engine.search_instances(kb_name, "", limit=500)
        own_instances = [inst for inst in instances if inst["design_id"] == design_id]
        if own_instances:
            with st.expander(f"模块 {selected} 关联的实例 (设计内 {len(own_instances)} 个)"):
                st.dataframe(own_instances, width="stretch", hide_index=True)


def _label(designs: list[dict], design_id: str) -> str:
    design = next((d for d in designs if d["design_id"] == design_id), None)
    if not design:
        return design_id
    return f"{design_id} · {design['module_count']} 模块"
