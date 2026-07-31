from __future__ import annotations

from datetime import datetime, timezone
import importlib.util
from pathlib import Path

import altair as alt
import pandas as pd

from src.core.auth import ROLE_SYSTEM_ADMIN
from src.evaluation.config import EvaluationConfig
from src.evaluation.dataset_loader import load_dataset
from src.evaluation.presentation import (
    build_baseline_chart_rows,
    build_comparison,
    build_credibility_summary,
    build_current_metric_chart_rows,
    build_sample_rows,
    classify_sample_result,
)
from src.evaluation.run_control import EvaluationRunController
from src.evaluation.schemas import EvaluationRunState, EvaluationSummary, SampleResult
from src.evaluation.service import EvaluationService


DEFAULT_DATASET = Path("evaluation/datasets/hardware_qa_v1.jsonl")
DEFAULT_OUTPUT_ROOT = Path("storage/evaluations")
ACTIVE_EVALUATION_RUN_KEY = "evaluation_active_run_id"


def can_access_evaluation(role: str | None) -> bool:
    return role == ROLE_SYSTEM_ADMIN


def load_evaluation_summary(path: str | Path) -> EvaluationSummary:
    return EvaluationSummary.model_validate_json(Path(path).read_text(encoding="utf-8-sig"))


def list_evaluation_runs(root: str | Path = DEFAULT_OUTPUT_ROOT) -> list[Path]:
    root = Path(root)
    if not root.exists():
        return []
    return sorted(
        [
            path
            for path in root.iterdir()
            if path.is_dir()
            and (
                (path / "summary.json").is_file()
                or (path / "run_state.json").is_file()
            )
        ],
        key=lambda path: path.name,
        reverse=True,
    )


def preflight_scoring() -> str | None:
    if importlib.util.find_spec("ragas") is None:
        return "缺少评估依赖；请运行 uv sync --group eval"
    try:
        EvaluationConfig.from_environment()
    except Exception as exc:
        return f"评估配置无效：{exc}"
    return None


def evaluation_service_factory():
    """Create services without evaluator config until a score-enabled run scores."""
    return EvaluationService


def run_action_availability(status: str) -> dict[str, bool]:
    return {
        "pause": status in {"queued", "running"},
        "resume": status in {"paused", "cancelled"},
        "cancel": status in {"queued", "running", "pause_requested", "paused"},
    }


def _load_results(path: Path) -> list[SampleResult]:
    if not path.exists():
        return []
    return [
        SampleResult.model_validate_json(line)
        for line in path.read_text(encoding="utf-8-sig").splitlines()
        if line.strip()
    ]


def should_render_evaluation_summary(run_dir: str | Path) -> bool:
    run_dir = Path(run_dir)
    if not (run_dir / "summary.json").is_file():
        return False
    state_path = run_dir / "run_state.json"
    if not state_path.is_file():
        return True
    state = EvaluationRunState.model_validate_json(state_path.read_text(encoding="utf-8"))
    if state.status == "completed":
        return True
    if state.status not in {"paused", "cancelled", "failed"}:
        return False
    summary = load_evaluation_summary(run_dir / "summary.json")
    return str(summary.metadata.get("run_outcome", {}).get("kind", "")).startswith("partial_")


def _run_outcome_message(summary: EvaluationSummary) -> tuple[str, str] | None:
    outcome = summary.metadata.get("run_outcome", {})
    kind = outcome.get("kind")
    if kind not in {"partial_cancelled", "partial_paused", "partial_failed"}:
        return None
    completed = outcome.get("completed_groups", 0)
    total = outcome.get("total_groups", 0)
    labels = {
        "partial_cancelled": "已取消",
        "partial_paused": "已暂停",
        "partial_failed": "发生异常",
    }
    return (
        f"部分评分报告：{labels[kind]}；已完成评分组 {completed} / {total}。"
        "已完成指标可供查看，未完成指标不应作为结论依据。",
        "warning",
    )


def _build_current_metric_chart(rows: list[dict[str, object]]):
    """Build a threshold-aware horizontal score chart for one evaluation run."""
    data = pd.DataFrame(rows)
    data["bar_color"] = data.apply(
        lambda row: (
            "#9ca3af"
            if pd.isna(row["threshold"])
            else "#82b366"
            if row["meets_threshold"]
            else "#b85450"
        ),
        axis=1,
    )
    bars = alt.Chart(data).mark_bar(cornerRadiusEnd=4).encode(
        x=alt.X("score:Q", scale=alt.Scale(domain=[0, 1]), title="得分（0-1）"),
        y=alt.Y("metric_label:N", sort=None, title=None),
        color=alt.Color("bar_color:N", scale=None, legend=None),
        tooltip=[
            "metric_label:N",
            "metric:N",
            alt.Tooltip("score:Q", format=".3f", title="得分"),
            alt.Tooltip("threshold:Q", format=".2f", title="门禁阈值"),
            alt.Tooltip("applicable_samples:Q", title="适用样本"),
            alt.Tooltip("scoring_failures:Q", title="评分失败"),
        ],
    )
    labels = alt.Chart(data).mark_text(
        align="left", baseline="middle", dx=4, color="#1f2937"
    ).encode(
        x=alt.X("score:Q"),
        y=alt.Y("metric_label:N", sort=None),
        text=alt.Text("score:Q", format=".3f"),
    )
    thresholds = (
        alt.Chart(data)
        .transform_filter("isValid(datum.threshold)")
        .mark_tick(color="#374151", thickness=2, size=30)
        .encode(
            x=alt.X("threshold:Q"),
            y=alt.Y("metric_label:N", sort=None),
        )
    )
    return bars + labels + thresholds


def _build_baseline_metric_chart(rows: list[dict[str, object]]):
    """Build grouped horizontal bars for the current run and its baseline."""
    comparison_data = pd.DataFrame(rows).melt(
        id_vars=["metric", "metric_label", "change"],
        value_vars=["current", "baseline"],
        var_name="run",
        value_name="score",
    )
    comparison_data["run"] = comparison_data["run"].replace(
        {"current": "当前", "baseline": "基线"}
    )
    bars = alt.Chart(comparison_data).mark_bar(cornerRadiusEnd=4).encode(
        x=alt.X("score:Q", scale=alt.Scale(domain=[0, 1]), title="得分（0-1）"),
        y=alt.Y("metric_label:N", sort=None, title=None),
        yOffset=alt.YOffset("run:N", sort=["当前", "基线"]),
        color=alt.Color(
            "run:N",
            sort=["当前", "基线"],
            scale=alt.Scale(domain=["当前", "基线"], range=["#4c78a8", "#9ca3af"]),
            title="运行",
        ),
        tooltip=[
            "metric_label:N",
            "metric:N",
            "run:N",
            alt.Tooltip("score:Q", format=".3f", title="得分"),
            alt.Tooltip("change:Q", format="+.3f", title="相对基线变化"),
        ],
    )
    labels = alt.Chart(comparison_data).mark_text(
        align="left", baseline="middle", dx=4, color="#1f2937"
    ).encode(
        x=alt.X("score:Q"),
        y=alt.Y("metric_label:N", sort=None),
        yOffset=alt.YOffset("run:N", sort=["当前", "基线"]),
        text=alt.Text("score:Q", format=".3f"),
    )
    return bars + labels


def render_saved_evaluation_run(
    st, controller: EvaluationRunController, output_root: str | Path
) -> bool:
    """Render the saved session run, returning whether it replaced the new-run form."""
    run_id = st.session_state.get(ACTIVE_EVALUATION_RUN_KEY)
    if not run_id:
        return False

    run_dir = Path(output_root) / str(run_id)
    if not run_dir.is_dir():
        st.session_state.pop(ACTIVE_EVALUATION_RUN_KEY, None)
        return False

    if st.button("开始新的评估", key="clear-active-evaluation"):
        st.session_state.pop(ACTIVE_EVALUATION_RUN_KEY, None)
        st.rerun()
        return True

    try:
        if should_render_evaluation_summary(run_dir):
            _render_summary(
                st,
                load_evaluation_summary(run_dir / "summary.json"),
                _load_results(run_dir / "results.jsonl"),
            )
        else:
            _render_active_status(st, controller, str(run_id))
    except (OSError, ValueError) as exc:
        st.error(f"Unable to load evaluation run state: {exc}")
    return True


def _render_summary(
    st,
    summary: EvaluationSummary,
    results: list[SampleResult],
    baseline: EvaluationSummary | None = None,
) -> None:
    outcome = _run_outcome_message(summary)
    if outcome is not None:
        message, level = outcome
        getattr(st, level)(message)
    credibility = build_credibility_summary(summary, results)
    columns = st.columns(5)
    columns[0].metric("样本", summary.sample_count)
    columns[1].metric("采集成功", summary.successful_samples)
    columns[2].metric("有检索证据", f"{credibility['evidence_samples']} / {len(results)}")
    columns[3].metric("已评分", f"{credibility['scored_samples']} / {len(results)}")
    columns[4].metric(
        "门禁",
        "通过" if summary.gate and summary.gate.passed else "未通过" if summary.gate else "未执行",
    )

    status = credibility["status"]
    status_text = (
        f"结果状态：{status}；采集失败 {credibility['collection_failures']} 条，"
        f"评分失败 {credibility['metric_failures']} 条。"
    )
    if status == "存在技术失败":
        st.error(status_text + "请先排除技术失败，再用分数作结论。")
    elif status == "评分覆盖不足":
        st.warning(status_text + "当前没有足够的有效评分用于比较。")
    else:
        st.info(status_text + "分数仍应结合适用样本数、证据和门禁原因解读。")

    current_chart_rows = build_current_metric_chart_rows(summary)
    st.subheader("当前评估效果")
    if current_chart_rows:
        st.altair_chart(
            _build_current_metric_chart(current_chart_rows), use_container_width=True
        )
    else:
        st.info("当前运行没有可展示的有效评分指标。")

    metric_rows = [
        {
            "指标": name,
            "得分": round(score, 4),
            "适用样本": summary.metric_counts.get(name, 0),
            "评分失败": summary.metric_failures.get(name, 0),
        }
        for name, score in sorted(summary.metric_scores.items())
    ]
    if metric_rows:
        st.subheader("指标与门禁")
        st.dataframe(metric_rows, width="stretch", hide_index=True)
    if summary.gate and summary.gate.failures:
        with st.expander("门禁失败原因", expanded=True):
            for failure in summary.gate.failures:
                st.write(f"- {failure}")

    if baseline is not None:
        comparison = build_comparison(summary, baseline)
        st.subheader(f"与 {baseline.run_id} 的历史对比")
        baseline_chart_rows = build_baseline_chart_rows(summary, baseline)
        if baseline_chart_rows:
            st.altair_chart(
                _build_baseline_metric_chart(baseline_chart_rows),
                use_container_width=True,
            )
        if comparison:
            st.dataframe(comparison, width="stretch", hide_index=True)
        else:
            st.info("两次运行没有可直接比较的同名指标。")

    sample_rows = build_sample_rows(results)
    states = sorted({row["状态"] for row in sample_rows})
    selected_state = st.selectbox(
        "样本状态筛选",
        ["全部", *states],
        key=f"result-status-{summary.run_id}",
    )
    filtered_pairs = [
        (result, row)
        for result, row in zip(results, sample_rows, strict=True)
        if selected_state == "全部" or row["状态"] == selected_state
    ]
    st.subheader("样本诊断")
    st.caption(
        f"显示 {len(filtered_pairs)} / {len(results)} 个样本；"
        "优先查看采集失败、评分失败和关键样本待复核。"
    )
    if filtered_pairs:
        st.dataframe([row for _, row in filtered_pairs], width="stretch", hide_index=True)
    for result, _row in filtered_pairs:
        with st.expander(f"{result.sample_id} · {classify_sample_result(result)}"):
            st.markdown("**问题**")
            st.write(result.question)
            st.markdown("**参考答案**")
            st.write(result.reference_answer)
            st.markdown("**实际回答**")
            st.write(result.response)
            st.markdown("**检索上下文**")
            st.write(result.retrieved_contexts or ["无"])
            retrieval_summary = result.metadata.get("retrieval_summary", {})
            if retrieval_summary:
                st.markdown("**检索与证据诊断**")
                st.caption(
                    "检索轮次："
                    f"{retrieval_summary.get('retrieval_rounds', 0)}；"
                    "最终证据数："
                    f"{retrieval_summary.get('final_top_k', 0)}；"
                    "充分性："
                    f"{retrieval_summary.get('sufficiency_status') or '未判定'}"
                )
                for title, key in (
                    ("声明覆盖", "claim_coverage"),
                    ("检索账本", "retrieval_ledger"),
                    ("证据质量", "evidence_quality"),
                ):
                    rows = retrieval_summary.get(key) or []
                    if rows:
                        st.markdown(f"*{title}*")
                        st.dataframe(rows, width="stretch", hide_index=True)
            result_metric_rows = [
                {
                    "指标": metric.metric_name,
                    "状态": metric.status,
                    "得分": metric.score,
                    "原因": metric.reason,
                }
                for metric in result.metrics
            ]
            if result_metric_rows:
                st.dataframe(result_metric_rows, width="stretch", hide_index=True)


def _elapsed_seconds(state: EvaluationRunState) -> float:
    if not state.started_at:
        return 0.0
    try:
        started_at = datetime.fromisoformat(state.started_at)
        finished_at = (
            datetime.fromisoformat(state.finished_at)
            if state.finished_at
            else datetime.now(timezone.utc)
        )
    except ValueError:
        return 0.0
    return max(0.0, (finished_at - started_at).total_seconds())


def _render_run_status(st, controller: EvaluationRunController, run_id: str) -> None:
    try:
        state = controller.load_for_display(run_id)
    except (OSError, ValueError) as exc:
        st.error(f"Unable to load evaluation run state: {exc}")
        return
    st.subheader("运行状态")
    st.write(f"运行 ID：{state.run_id}")
    st.write(f"状态：{state.status}；阶段：{state.stage}")
    st.write(f"当前样本：{state.current_sample_id or '无'}")
    st.write(f"当前问题：{state.current_question or '无'}")
    if state.stage == "scoring":
        st.write(
            "评分进度："
            f"{state.scoring_completed_groups} / {state.scoring_total_groups or '—'} 个指标组"
        )
    progress = state.completed_samples / state.total_samples if state.total_samples else 0.0
    st.progress(progress, text=f"{state.completed_samples} / {state.total_samples}")
    columns = st.columns(3)
    columns[0].metric("成功", state.successful_samples)
    columns[1].metric("失败", state.failed_samples)
    columns[2].metric("耗时", f"{_elapsed_seconds(state):.1f} 秒")
    if state.error_message:
        st.error(state.error_message)
    st.caption(f"运行目录：{controller.state_root / state.run_id}")
    st.caption(f"快照路径：{state.snapshot_path}")

    actions = run_action_availability(state.status)
    pause_column, resume_column, cancel_column = st.columns(3)
    if pause_column.button("暂停", key=f"pause-{state.run_id}", disabled=not actions["pause"]):
        controller.pause(state.run_id)
        st.rerun()
    if resume_column.button("继续", key=f"resume-{state.run_id}", disabled=not actions["resume"]):
        controller.resume(state.run_id)
        controller.start(state.run_id)
        st.rerun()
    if cancel_column.button("取消", key=f"cancel-{state.run_id}", disabled=not actions["cancel"]):
        controller.cancel(state.run_id)
        st.rerun()


def _render_active_status(st, controller: EvaluationRunController, run_id: str) -> None:
    @st.fragment(run_every="2s")
    def status_panel() -> None:
        _render_run_status(st, controller, run_id)

    status_panel()


def render_evaluation_page(current_role: str | None) -> None:
    import streamlit as st

    st.title("RAGAS 评估")
    if not can_access_evaluation(current_role):
        st.error("只有系统管理员可以访问 RAGAS 评估。")
        return

    @st.cache_resource
    def get_controller(output_root: str) -> EvaluationRunController:
        return EvaluationRunController(
            evaluation_service_factory(),
            output_root,
        )

    output_root = Path(st.text_input("评估输出目录", str(DEFAULT_OUTPUT_ROOT)))
    controller = get_controller(str(output_root))
    mode = st.radio(
        "模式",
        ["在线采集并评分", "离线重评快照", "查看历史报告"],
        horizontal=True,
    )
    if mode != "查看历史报告" and render_saved_evaluation_run(
        st, controller, output_root
    ):
        return

    if mode == "查看历史报告":
        runs = list_evaluation_runs(output_root)
        if not runs:
            st.info("尚无评估运行。")
            return
        selected = st.selectbox("运行", runs, format_func=lambda path: path.name)
        st.session_state[ACTIVE_EVALUATION_RUN_KEY] = selected.name
        try:
            if should_render_evaluation_summary(selected):
                baseline_candidates = [
                    run
                    for run in runs
                    if run != selected and should_render_evaluation_summary(run)
                ]
                baseline = None
                if baseline_candidates:
                    selected_baseline = st.selectbox(
                        "历史对比基线",
                        baseline_candidates,
                        format_func=lambda path: path.name,
                    )
                    baseline = load_evaluation_summary(selected_baseline / "summary.json")
                _render_summary(
                    st,
                    load_evaluation_summary(selected / "summary.json"),
                    _load_results(selected / "results.jsonl"),
                    baseline,
                )
            else:
                _render_active_status(st, controller, selected.name)
        except (OSError, ValueError) as exc:
            st.error(f"Unable to load evaluation run state: {exc}")
        return

    uploaded = st.file_uploader(
        "上传 JSONL（留空使用内置 25 条数据集）",
        type=["jsonl"],
    )
    dataset_path = DEFAULT_DATASET
    if uploaded is not None:
        upload_dir = output_root / "uploads"
        upload_dir.mkdir(parents=True, exist_ok=True)
        dataset_path = upload_dir / Path(uploaded.name).name
        dataset_path.write_bytes(uploaded.getvalue())
    samples = load_dataset(dataset_path)
    all_tags = sorted({tag for sample in samples for tag in sample.tags})
    selected_tags = set(st.multiselect("标签筛选", all_tags))
    selected_ids = set(st.multiselect("样本筛选", [sample.id for sample in samples]))
    if selected_tags:
        samples = [sample for sample in samples if selected_tags.intersection(sample.tags)]
    if selected_ids:
        samples = [sample for sample in samples if sample.id in selected_ids]
    st.caption(f"将运行 {len(samples)} 条样本")

    snapshot_path = None
    score_enabled = True
    if mode == "离线重评快照":
        raw_snapshot = st.text_input("快照 JSONL 路径")
        snapshot_path = Path(raw_snapshot) if raw_snapshot else None
    else:
        score_enabled = st.checkbox("启用 RAGAS 评分", value=True)
    disabled = not samples or (mode == "离线重评快照" and snapshot_path is None)
    if not st.button("开始评估", type="primary", disabled=disabled):
        return

    if mode == "离线重评快照" or score_enabled:
        error = preflight_scoring()
        if error:
            st.error(error)
            return
    try:
        if mode == "在线采集并评分":
            state = controller.create_online_run(
                dataset_path,
                output_root,
                samples,
                score_enabled=score_enabled,
                sample_ids=selected_ids,
                tags=selected_tags,
            )
        else:
            state = controller.create_offline_run(
                dataset_path,
                output_root,
                samples,
                snapshot_path,
                sample_ids=selected_ids,
                tags=selected_tags,
            )
        st.session_state[ACTIVE_EVALUATION_RUN_KEY] = state.run_id
        controller.start(state.run_id)
        _render_active_status(st, controller, state.run_id)
    except Exception as exc:
        st.error(f"评估失败：{exc}")
