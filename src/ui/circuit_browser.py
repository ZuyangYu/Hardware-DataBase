from __future__ import annotations

import os

import streamlit as st

import config.settings
from src.circuit.index_service import CircuitIndexService
from src.circuit.query_engine import CircuitQueryEngine
from src.circuit.store import CircuitStore


PARSE_LOG_FILENAME = "parse.log"
PARSE_LOG_TAIL_BYTES = 64 * 1024  # show at most the trailing 64 KB


def _parse_log_path(kb_name: str, design_id: str) -> str:
    return os.path.join(CircuitStore().design_dir(kb_name, design_id), PARSE_LOG_FILENAME)


def _render_parse_log(kb_name: str, design_id: str) -> None:
    path = _parse_log_path(kb_name, design_id)
    if not os.path.exists(path):
        st.caption("尚无解析日志（仅当解析过程被记录后才会生成）。")
        return
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if size > PARSE_LOG_TAIL_BYTES:
                fh.seek(-PARSE_LOG_TAIL_BYTES, os.SEEK_END)
                prefix = "…（已截断，仅显示最近 64KB）\n\n"
            else:
                prefix = ""
            content = prefix + fh.read().decode("utf-8", errors="replace")
    except OSError as exc:
        st.warning(f"读取解析日志失败：{exc}")
        return
    st.caption(f"日志路径：{path}（{size} bytes）")
    st.code(content, language="json")


def _list_design_dirs_with_log(kb_name: str) -> list[tuple[str, str]]:
    """Return ``[(design_id, parse_log_path), …]`` for every design dir that
    carries a ``parse.log`` — even when ``circuit_state.json`` is missing
    because the parse failed before the orchestrator could persist a design.
    """
    store = CircuitStore()
    try:
        kb_root = os.path.join(store.root, kb_name)
    except Exception:
        return []
    if not os.path.isdir(kb_root):
        return []
    entries = []
    for name in sorted(os.listdir(kb_root)):
        candidate = os.path.join(kb_root, name, PARSE_LOG_FILENAME)
        if os.path.exists(candidate):
            entries.append((name, candidate))
    return entries


def _delete_circuit_upload_archive(kb_name: str, design_id: str) -> None:
    """Best-effort cleanup of any archived raw upload files matching design_id.

    The archive layout is ``storage/circuit_uploads/<kb>/<source_group>/<file>``.
    File stems are normalised to design_id via ``make_design_id``; match by
    stem prefix to catch the ``_<timestamp>`` variants written when names clash.
    """
    archive_root = os.path.join(config.settings.STORAGE_DIR, "circuit_uploads", kb_name)
    if not os.path.isdir(archive_root):
        return
    from src.circuit.store import make_design_id  # local import: avoid cycle at module load

    for group_name in os.listdir(archive_root):
        group_dir = os.path.join(archive_root, group_name)
        if not os.path.isdir(group_dir):
            continue
        for fname in os.listdir(group_dir):
            stem, _ext = os.path.splitext(fname)
            if make_design_id(stem) == design_id or make_design_id(stem).startswith(design_id + "_"):
                try:
                    os.remove(os.path.join(group_dir, fname))
                except OSError:
                    pass


def _delete_design(kb_name: str, design_id: str) -> tuple[bool, str]:
    """Delete one circuit design end-to-end: design dir, global index entry,
    archived raw upload files. Returns ``(ok, message)``.
    """
    try:
        removed = CircuitIndexService().delete_design(kb_name, design_id)
    except Exception as exc:
        return False, f"删除设计目录失败: {exc}"
    try:
        _delete_circuit_upload_archive(kb_name, design_id)
    except Exception as exc:
        # archive cleanup is best-effort
        return True, f"已删除设计 {design_id}，但归档清理部分失败: {exc}"
    if not removed:
        return False, f"未找到要删除的设计: {design_id}"
    return True, f"已删除设计: {design_id}"


def render_circuit_browser(
    kb_name: str,
    key_prefix: str = "circuit_browser",
    can_write: bool = False,
    on_deleted=None,
):
    """Render the circuit-design browser for ``kb_name``.

    Parameters
    ----------
    can_write:
        When True, show a 🗑️ delete button next to the selected design and the
        failed-parse entries. The caller is responsible for verifying KB-level
        ``write`` permission before passing True.
    on_deleted:
        Optional callback ``(kb_name, design_id) -> None`` invoked after a
        successful delete so the caller can write an audit record / toast.
    """
    engine = CircuitQueryEngine()
    designs = engine.list_designs(kb_name)
    failed_logs = [
        (design_id, path)
        for design_id, path in _list_design_dirs_with_log(kb_name)
        if design_id not in {d["design_id"] for d in designs}
    ]
    confirm_key = f"{key_prefix}_confirm_delete_design"
    if confirm_key not in st.session_state:
        st.session_state[confirm_key] = None

    with st.container(border=True):
        st.markdown("##### 🔌 电路设计浏览")
        if not designs and not failed_logs:
            st.caption("当前知识库尚未解析 EDF 网表或 PDF 原理图。")
            return

        if failed_logs:
            with st.expander(
                f"⚠️ 有 {len(failed_logs)} 份文件解析失败 / 未能写出设计状态（点击查看解析日志）"
            ):
                failed_ids = [design_id for design_id, _ in failed_logs]
                selected_failed = st.selectbox(
                    "失败设计",
                    failed_ids,
                    key=f"{key_prefix}_failed_design",
                )
                _render_parse_log(kb_name, selected_failed)
                if can_write and selected_failed:
                    _render_delete_controls(
                        kb_name,
                        selected_failed,
                        scope="failed",
                        key_prefix=key_prefix,
                        confirm_key=confirm_key,
                        on_deleted=on_deleted,
                    )

        if not designs:
            return

        col_sel, col_del = st.columns([5, 1]) if can_write else (st.container(), None)
        with col_sel:
            selected_design_id = st.selectbox(
                "设计",
                [design["design_id"] for design in designs],
                format_func=lambda design_id: _format_design_label(designs, design_id),
                key=f"{key_prefix}_design",
            )
        if can_write and col_del is not None and selected_design_id:
            with col_del:
                _render_delete_controls(
                    kb_name,
                    selected_design_id,
                    scope="active",
                    key_prefix=key_prefix,
                    confirm_key=confirm_key,
                    on_deleted=on_deleted,
                )
        summary = engine.get_design_summary(kb_name, selected_design_id)
        if not summary:
            st.caption("未找到该设计的结构化结果。")
            return

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("实例", summary["instances"])
        c2.metric("网络", summary["nets"])
        c3.metric("模块", len(summary["modules"]))
        c4.metric("状态", summary["status"])

        tab_modules, tab_nets, tab_instances, tab_refs = st.tabs(["模块", "网络", "实例", "EDF/PDF 对应"])
        with tab_modules:
            if summary["modules"]:
                st.dataframe(summary["modules"], width="stretch", hide_index=True)
            else:
                st.caption("暂无模块划分结果。")
        with tab_nets:
            net_query = st.text_input("网络名过滤", key=f"{key_prefix}_net_query", placeholder="例如 VCC3V3 / GND / CLK")
            nets = engine.search_nets(kb_name, net_query, limit=100)
            if nets:
                st.dataframe(nets, width="stretch", hide_index=True)
            else:
                st.caption("没有匹配的网络。")
        with tab_instances:
            instance_query = st.text_input("实例过滤", key=f"{key_prefix}_instance_query", placeholder="例如 U100 / TPS / 872...")
            instances = engine.search_instances(kb_name, instance_query, limit=100)
            if instances:
                st.dataframe(instances, width="stretch", hide_index=True)
            else:
                st.caption("没有匹配的实例。")
        with tab_refs:
            refs = engine.search_cross_references(kb_name, limit=100)
            if refs:
                st.dataframe(refs, width="stretch", hide_index=True)
            else:
                st.caption("尚未建立 EDF/PDF 对应关系。上传同一知识库下的 EDF 和原理图 PDF 后会自动尝试匹配。")

        if summary["warnings"]:
            with st.expander("解析提示"):
                for warning in summary["warnings"]:
                    st.caption(warning)

        with st.expander("解析日志（每次上传记录解析过程 / 失败 traceback）"):
            _render_parse_log(kb_name, selected_design_id)


def _render_delete_controls(
    kb_name: str,
    design_id: str,
    *,
    scope: str,
    key_prefix: str,
    confirm_key: str,
    on_deleted,
) -> None:
    """Render the 🗑️ / ✓ / ✗ flow for one design id."""
    pending = st.session_state.get(confirm_key)
    is_confirming = pending == (kb_name, design_id, scope)
    if is_confirming:
        sub1, sub2 = st.columns([1, 1])
        with sub1:
            if st.button(
                "✓",
                key=f"{key_prefix}_yes_del_{scope}_{design_id}",
                help="确认删除该设计及其解析产物",
            ):
                with st.spinner("删除中..."):
                    ok, msg = _delete_design(kb_name, design_id)
                st.session_state[confirm_key] = None
                if ok:
                    if callable(on_deleted):
                        try:
                            on_deleted(kb_name, design_id)
                        except Exception:
                            pass
                    st.session_state["toast_msg"] = msg
                else:
                    st.session_state["error_msg"] = msg
                st.rerun()
        with sub2:
            if st.button("✗", key=f"{key_prefix}_no_del_{scope}_{design_id}", help="取消"):
                st.session_state[confirm_key] = None
                st.rerun()
    else:
        if st.button(
            "🗑️ 删除设计",
            key=f"{key_prefix}_del_{scope}_{design_id}",
            help="删除该设计的解析状态、模块截图、PDF 缓存与原始归档",
        ):
            st.session_state[confirm_key] = (kb_name, design_id, scope)
            st.rerun()


def _format_design_label(designs: list[dict], design_id: str) -> str:
    design = next((item for item in designs if item["design_id"] == design_id), None)
    if not design:
        return design_id
    return (
        f"{design_id} · {design['status']} · "
        f"{design['instance_count']} 实例 / {design['net_count']} 网络 / {design['module_count']} 模块"
    )
