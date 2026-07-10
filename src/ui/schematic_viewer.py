"""Schematic-page viewer.

Plan section 11.1 / 6.x asks for a viewer that surfaces schematic PDF pages
along with the extracted labels and known module regions. The full
high-light overlay is a frontend story; for v1 we render the structured
extraction so the user can inspect what the parser saw without leaving
Streamlit.
"""

from __future__ import annotations

import streamlit as st

from src.circuit.store import CircuitStore


def render_schematic_viewer(kb_name: str, key_prefix: str = "schematic_viewer"):
    store = CircuitStore()
    designs = store.list_designs(kb_name)
    with st.container(border=True):
        st.markdown("##### 🗺 原理图查看")
        designs_with_pdf = [d for d in designs if d.schematic_pages]
        if not designs_with_pdf:
            st.caption("当前知识库尚无原理图 PDF 解析结果。")
            return

        design = st.selectbox(
            "设计",
            designs_with_pdf,
            format_func=lambda d: f"{d.design_id} · {len(d.schematic_pages)} 页",
            key=f"{key_prefix}_design",
        )
        page_numbers = [page.page_number for page in design.schematic_pages]
        page_number = st.selectbox("页码", page_numbers, key=f"{key_prefix}_page")
        page = next(p for p in design.schematic_pages if p.page_number == page_number)

        c1, c2, c3 = st.columns(3)
        c1.metric("标签", len(page.labels))
        c2.metric("宽度", f"{page.width:.0f}" if page.width else "—")
        c3.metric("高度", f"{page.height:.0f}" if page.height else "—")

        with st.expander("提取到的文本", expanded=False):
            st.text(page.text or "（该页未提取到文本）")

        if page.labels:
            st.caption("文本标签")
            st.dataframe(
                [
                    {
                        "text": label.text,
                        "kind": label.kind,
                        "bbox": label.bbox,
                    }
                    for label in page.labels[:200]
                ],
                width="stretch",
                hide_index=True,
            )

        page_regions = [r for r in design.module_regions if r.page_number == page_number]
        if page_regions:
            st.caption("候选模块区域")
            st.dataframe(
                [
                    {
                        "module_id": region.module_id,
                        "bbox": region.bbox,
                        "confidence": region.confidence,
                        "strategy": region.strategy,
                    }
                    for region in page_regions
                ],
                width="stretch",
                hide_index=True,
            )

        screenshots = store.list_module_screenshots(kb_name, design.design_id)
        pdf_cache = store.list_pdf_cache(kb_name, design.design_id)
        if screenshots or pdf_cache:
            with st.expander("已生成的图像与缓存", expanded=False):
                if pdf_cache:
                    st.caption(f"📄 PDF 缓存（{len(pdf_cache)}）")
                    for path in pdf_cache:
                        st.code(path, language="text")
                if screenshots:
                    st.caption(f"🖼 模块截图（{len(screenshots)}）")
                    for path in screenshots:
                        if path.endswith(".png") or path.endswith(".jpg"):
                            try:
                                st.image(path, caption=path)
                            except Exception:
                                st.code(path, language="text")
                        else:
                            st.code(path, language="text")
