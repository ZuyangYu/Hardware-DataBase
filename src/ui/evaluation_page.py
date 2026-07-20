from __future__ import annotations

from pathlib import Path

from src.core.auth import ROLE_SYSTEM_ADMIN
from src.evaluation.config import EvaluationConfig
from src.evaluation.dataset_loader import load_dataset
from src.evaluation.reporters import write_reports
from src.evaluation.schemas import EvaluationSummary, SampleResult
from src.evaluation.service import EvaluationService, new_run_id
from src.evaluation.snapshot_store import SnapshotStore

DEFAULT_DATASET = Path("evaluation/datasets/hardware_qa_v1.jsonl")
DEFAULT_OUTPUT_ROOT = Path("storage/evaluations")


def can_access_evaluation(role: str | None) -> bool:
    return role == ROLE_SYSTEM_ADMIN


def load_evaluation_summary(path: str | Path) -> EvaluationSummary:
    return EvaluationSummary.model_validate_json(Path(path).read_text(encoding="utf-8-sig"))


def list_evaluation_runs(root: str | Path = DEFAULT_OUTPUT_ROOT) -> list[Path]:
    root = Path(root)
    if not root.exists():
        return []
    return sorted(
        [path for path in root.iterdir() if path.is_dir() and (path / "summary.json").is_file()],
        key=lambda path: path.name,
        reverse=True,
    )


def _load_results(path: Path) -> list[SampleResult]:
    if not path.exists():
        return []
    return [
        SampleResult.model_validate_json(line)
        for line in path.read_text(encoding="utf-8-sig").splitlines()
        if line.strip()
    ]


def _render_summary(st, summary: EvaluationSummary, results: list[SampleResult]) -> None:
    columns = st.columns(4)
    columns[0].metric("样本", summary.sample_count)
    columns[1].metric("成功", summary.successful_samples)
    columns[2].metric("失败", summary.failed_samples)
    columns[3].metric("门禁", "通过" if summary.gate and summary.gate.passed else "未通过")
    rows = [
        {
            "指标": name,
            "得分": round(score, 4),
            "适用样本": summary.metric_counts.get(name, 0),
            "失败": summary.metric_failures.get(name, 0),
        }
        for name, score in sorted(summary.metric_scores.items())
    ]
    if rows:
        st.dataframe(rows, width="stretch", hide_index=True)
    if summary.gate and summary.gate.failures:
        with st.expander("门禁失败原因", expanded=True):
            for failure in summary.gate.failures:
                st.write(f"- {failure}")
    for result in results:
        with st.expander(result.sample_id):
            st.markdown("**问题**")
            st.write(result.question)
            st.markdown("**参考答案**")
            st.write(result.reference_answer)
            st.markdown("**实际回答**")
            st.write(result.response)
            st.markdown("**检索上下文**")
            st.write(result.retrieved_contexts or ["无"])
            st.json(
                {
                    metric.metric_name: metric.score if metric.score is not None else metric.status
                    for metric in result.metrics
                }
            )


def render_evaluation_page(current_role: str | None) -> None:
    import streamlit as st

    st.title("RAGAS 评估")
    if not can_access_evaluation(current_role):
        st.error("只有系统管理员可以访问 RAGAS 评估。")
        return
    mode = st.radio("模式", ["在线采集并评分", "离线重评快照", "查看历史报告"], horizontal=True)
    output_root = Path(st.text_input("评估输出目录", str(DEFAULT_OUTPUT_ROOT)))
    if mode == "查看历史报告":
        runs = list_evaluation_runs(output_root)
        if not runs:
            st.info("尚无评估报告。")
            return
        selected = st.selectbox("运行", runs, format_func=lambda path: path.name)
        _render_summary(st, load_evaluation_summary(selected / "summary.json"), _load_results(selected / "results.jsonl"))
        return
    uploaded = st.file_uploader("上传 JSONL（留空使用内置 25 条数据集）", type=["jsonl"])
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
    if mode == "离线重评快照":
        raw_snapshot = st.text_input("快照 JSONL 路径")
        snapshot_path = Path(raw_snapshot) if raw_snapshot else None
    disabled = not samples or (mode == "离线重评快照" and snapshot_path is None)
    if not st.button("开始评估", type="primary", disabled=disabled):
        return
    try:
        service = EvaluationService(config=EvaluationConfig.from_environment())
        with st.status("正在运行评估……", expanded=True) as status:
            run_id = new_run_id()
            run_dir = output_root / run_id
            if mode == "在线采集并评分":
                snapshots = service.collect(samples, run_dir / "snapshot.jsonl")
            else:
                snapshots = SnapshotStore(snapshot_path).load_all()
            summary, results = service.score(samples, snapshots, run_id=run_id)
            write_reports(run_dir, summary, results)
            status.update(label="评估完成", state="complete")
        _render_summary(st, summary, results)
    except Exception as exc:
        st.error(f"评估失败：{exc}")
