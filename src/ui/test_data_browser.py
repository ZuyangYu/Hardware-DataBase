"""Test-data browser UI."""

from __future__ import annotations

import streamlit as st

from src.test_data.query_engine import TestDataQueryEngine


def render_test_data_browser(kb_name: str, key_prefix: str = "test_data_browser"):
    engine = TestDataQueryEngine()
    reports = engine.list_reports(kb_name)
    with st.container(border=True):
        st.markdown("##### 🧪 测试数据浏览")
        if not reports:
            st.caption("当前知识库尚未上传测试报告。")
            return
        st.dataframe(reports, width="stretch", hide_index=True)
        query = st.text_input(
            "测量过滤",
            placeholder="例如 VOUT / efficiency / 3V3",
            key=f"{key_prefix}_query",
        )
        measurements = engine.search_measurements(kb_name, query, limit=100)
        if measurements:
            st.dataframe(measurements, width="stretch", hide_index=True)
        else:
            st.caption("没有匹配的测量值。")
