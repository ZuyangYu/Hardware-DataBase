import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.evaluation.run_control import EvaluationRunController, RunStateStore
from src.evaluation.schemas import (
    EvaluationRunState,
    EvaluationSample,
    EvaluationSummary,
    MetricResult,
    SampleResult,
)
from src.ui.evaluation_page import (
    ACTIVE_EVALUATION_RUN_KEY,
    _build_baseline_metric_chart,
    _build_current_metric_chart,
    can_access_evaluation,
    evaluation_service_factory,
    list_evaluation_runs,
    load_evaluation_summary,
    preflight_scoring,
    render_saved_evaluation_run,
    _render_summary,
    _render_run_status,
    _run_outcome_message,
    render_evaluation_page,
    run_action_availability,
    should_render_evaluation_summary,
)


class EvaluationPageTests(unittest.TestCase):
    def _write_state(self, root: Path, status: str, name: str) -> None:
        run = root / name
        run.mkdir()
        (run / "run_state.json").write_text(
            json.dumps({"run_id": name, "status": status}), encoding="utf-8"
        )

    def _write_summary(self, root: Path, name: str) -> None:
        run = root / name
        run.mkdir()
        (run / "summary.json").write_text(json.dumps({"run_id": name}), encoding="utf-8")

    def test_only_system_admin_can_access(self):
        self.assertTrue(can_access_evaluation("system_admin"))
        self.assertFalse(can_access_evaluation("dept_admin"))
        self.assertFalse(can_access_evaluation("user"))
        self.assertFalse(can_access_evaluation(None))

    def test_load_summary_validates_schema(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "summary.json"
            path.write_text(
                json.dumps({"run_id": "run-1", "sample_count": 2, "successful_samples": 2}),
                encoding="utf-8",
            )
            summary = load_evaluation_summary(path)
            self.assertEqual(summary.run_id, "run-1")
            self.assertEqual(summary.sample_count, 2)

    def test_list_runs_includes_completed_and_incomplete(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write_state(root, "running", "20260103T000000Z-c")
            self._write_summary(root, "20260102T000000Z-b")
            self._write_state(root, "paused", "20260101T000000Z-a")
            (root / "incomplete").mkdir()

            runs = list_evaluation_runs(root)

            self.assertEqual(
                [path.name for path in runs],
                ["20260103T000000Z-c", "20260102T000000Z-b", "20260101T000000Z-a"],
            )

    def test_control_availability_matches_status(self):
        self.assertEqual(
            run_action_availability("running"),
            {"pause": True, "resume": False, "cancel": True},
        )

    def test_saved_completed_run_renders_summary_after_rerun(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write_summary(root, "run-1")
            st = Mock()
            st.session_state = {ACTIVE_EVALUATION_RUN_KEY: "run-1"}
            st.button.return_value = False
            controller = Mock()

            with patch("src.ui.evaluation_page._render_summary") as render_summary:
                rendered = render_saved_evaluation_run(st, controller, root)

            self.assertTrue(rendered)
            self.assertEqual(render_summary.call_args.args[1].run_id, "run-1")

    def test_partial_terminal_summary_renders_but_running_summary_stays_active(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for status in ("paused", "cancelled", "failed", "running"):
                run = root / status
                run.mkdir()
                (run / "summary.json").write_text(
                    json.dumps(
                        {
                            "run_id": status,
                            "metadata": {
                                "run_outcome": {
                                    "kind": "partial_failed",
                                    "completed_groups": 1,
                                    "total_groups": 5,
                                }
                            },
                        }
                    ),
                    encoding="utf-8",
                )
                state = EvaluationRunState.new_online(
                    run_id=status,
                    dataset_path="dataset.jsonl",
                    snapshot_path="snapshot.jsonl",
                    total_samples=1,
                    score_enabled=True,
                ).model_copy(update={"status": status})
                (run / "run_state.json").write_text(state.model_dump_json(), encoding="utf-8")
                (run / "report_complete.json").write_text("{}\n", encoding="utf-8")

            self.assertTrue(should_render_evaluation_summary(root / "paused"))
            self.assertTrue(should_render_evaluation_summary(root / "cancelled"))
            self.assertTrue(should_render_evaluation_summary(root / "failed"))
            self.assertFalse(should_render_evaluation_summary(root / "running"))

    def test_partial_outcome_message_includes_progress(self):
        message, level = _run_outcome_message(
            EvaluationSummary(
                run_id="run-1",
                metadata={
                    "run_outcome": {
                        "kind": "partial_failed",
                        "completed_groups": 2,
                        "total_groups": 5,
                    }
                },
            )
        )

        self.assertEqual(level, "warning")
        self.assertIn("部分评分", message)
        self.assertIn("2 / 5", message)

    def test_partial_terminal_summary_without_completion_marker_stays_active(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            run = Path(temp_dir) / "run-1"
            run.mkdir()
            state = EvaluationRunState.new_online(
                run_id="run-1", dataset_path="dataset.jsonl", snapshot_path="snapshot.jsonl",
                total_samples=1, score_enabled=True,
            ).model_copy(update={"status": "failed"})
            (run / "run_state.json").write_text(state.model_dump_json(), encoding="utf-8")
            (run / "summary.json").write_text(
                json.dumps({"run_id": "run-1", "metadata": {"run_outcome": {"kind": "partial_failed"}}}),
                encoding="utf-8",
            )

            self.assertFalse(should_render_evaluation_summary(run))

    def test_missing_saved_run_is_cleared_and_returns_to_new_form(self):
        st = Mock()
        st.session_state = {ACTIVE_EVALUATION_RUN_KEY: "missing-run"}

        rendered = render_saved_evaluation_run(st, Mock(), Path("missing-root"))

        self.assertFalse(rendered)
        self.assertNotIn(ACTIVE_EVALUATION_RUN_KEY, st.session_state)

    def test_saved_active_run_renders_existing_status_panel(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write_state(root, "running", "run-1")
            st = Mock()
            st.session_state = {ACTIVE_EVALUATION_RUN_KEY: "run-1"}
            st.button.return_value = False
            controller = Mock()

            with patch("src.ui.evaluation_page._render_active_status") as render_status:
                rendered = render_saved_evaluation_run(st, controller, root)

            self.assertTrue(rendered)
            render_status.assert_called_once_with(st, controller, "run-1")

    def test_starting_online_run_saves_active_run_id(self):
        sample = EvaluationSample(
            id="q1",
            question="Question",
            reference_answer="Answer",
            kb_name="hardware",
        )

        class FakeStreamlit:
            def __init__(self):
                self.session_state = {}

            def title(self, *_args, **_kwargs):
                pass

            def cache_resource(self, function):
                return function

            def text_input(self, *_args, **_kwargs):
                return "temporary-output"

            def radio(self, *_args, **_kwargs):
                return "在线采集并评分"

            def file_uploader(self, *_args, **_kwargs):
                return None

            def multiselect(self, *_args, **_kwargs):
                return []

            def caption(self, *_args, **_kwargs):
                pass

            def checkbox(self, *_args, **_kwargs):
                return True

            def button(self, *_args, **_kwargs):
                return True

            def error(self, *_args, **_kwargs):
                pass

        st = FakeStreamlit()
        controller = Mock()
        controller.create_online_run.return_value = SimpleNamespace(run_id="run-1")

        with (
            patch.dict(sys.modules, {"streamlit": st}),
            patch("src.ui.evaluation_page.EvaluationRunController", return_value=controller),
            patch("src.ui.evaluation_page.load_dataset", return_value=[sample]),
            patch("src.ui.evaluation_page.preflight_scoring", return_value=None),
            patch("src.ui.evaluation_page._render_active_status"),
        ):
            render_evaluation_page("system_admin")

        self.assertEqual(st.session_state[ACTIVE_EVALUATION_RUN_KEY], "run-1")
        controller.start.assert_called_once_with("run-1")
        self.assertEqual(
            run_action_availability("paused"),
            {"pause": False, "resume": True, "cancel": True},
        )
        self.assertEqual(
            run_action_availability("completed"),
            {"pause": False, "resume": False, "cancel": False},
        )

    def test_pause_requested_run_keeps_cancel_available(self):
        self.assertEqual(
            run_action_availability("pause_requested"),
            {"pause": False, "resume": False, "cancel": True},
        )

    def test_run_status_shows_error_when_run_state_is_invalid(self):
        st = Mock()
        controller = Mock()
        controller.load_for_display.side_effect = ValueError("invalid run_state.json")

        _render_run_status(st, controller, "run-1")

        st.error.assert_called_once_with(
            "Unable to load evaluation run state: invalid run_state.json"
        )

    def test_failed_run_with_summary_does_not_render_completed_summary(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            run = Path(temp_dir) / "run-1"
            run.mkdir()
            state = EvaluationRunState.new_online(
                run_id="run-1",
                dataset_path="dataset.jsonl",
                snapshot_path="snapshot.jsonl",
                total_samples=1,
                score_enabled=True,
            ).model_copy(update={"status": "failed"})
            (run / "run_state.json").write_text(
                state.model_dump_json(), encoding="utf-8"
            )
            (run / "summary.json").write_text(
                json.dumps({"run_id": "run-1"}), encoding="utf-8"
            )

            class FakeStreamlit:
                session_state = {}

                def title(self, *_args, **_kwargs):
                    pass

                def cache_resource(self, function):
                    return function

                def radio(self, *_args, **_kwargs):
                    return "查看历史报告"

                def text_input(self, *_args, **_kwargs):
                    return temp_dir

                def info(self, *_args, **_kwargs):
                    pass

                def selectbox(self, *_args, **_kwargs):
                    return run

            with (
                patch.dict(sys.modules, {"streamlit": FakeStreamlit()}),
                patch("src.ui.evaluation_page._render_summary") as render_summary,
                patch("src.ui.evaluation_page._render_active_status") as render_status,
            ):
                render_evaluation_page("system_admin")

            render_summary.assert_not_called()
            render_status.assert_called_once()

    def test_summary_surfaces_technical_failures_and_sample_diagnostics(self):
        class FakeColumn:
            def __init__(self):
                self.metrics = []

            def metric(self, label, value):
                self.metrics.append((label, value))

        class FakeExpander:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        class FakeStreamlit:
            def __init__(self):
                self.errors = []
                self.dataframes = []

            def columns(self, count):
                return [FakeColumn() for _ in range(count)]

            def error(self, message):
                self.errors.append(message)

            def warning(self, *_args):
                pass

            def info(self, *_args):
                pass

            def subheader(self, *_args):
                pass

            def altair_chart(self, *_args, **_kwargs):
                pass

            def dataframe(self, data, **_kwargs):
                self.dataframes.append(data)

            def expander(self, *_args, **_kwargs):
                return FakeExpander()

            def selectbox(self, *_args, **_kwargs):
                return "全部"

            def caption(self, *_args):
                pass

            def markdown(self, *_args):
                pass

            def write(self, *_args):
                pass

        st = FakeStreamlit()
        summary = EvaluationSummary(
            run_id="run-1",
            sample_count=1,
            failed_samples=1,
            metric_scores={"faithfulness": 0.6},
        )
        result = SampleResult(
            sample_id="q1",
            snapshot_status="failed",
            metadata={
                "retrieval_summary": {
                    "retrieval_rounds": 1,
                    "final_top_k": 2,
                    "sufficiency_status": "sufficient",
                    "claim_coverage": [{"claim_id": "c1", "status": "supported"}],
                    "retrieval_ledger": [{"sub_question_id": "sq1", "status": "covered"}],
                    "evidence_quality": [{"evidence_id": "e1", "score": 0.9}],
                }
            },
            metrics=[
                MetricResult(
                    sample_id="q1", metric_name="faithfulness", status="failed"
                )
            ],
        )

        _render_summary(st, summary, [result])

        self.assertTrue(any("存在技术失败" in message for message in st.errors))
        self.assertTrue(
            any(
                "采集失败" in [row.get("状态") for row in dataframe]
                for dataframe in st.dataframes
            )
        )
        self.assertTrue(
            any(
                "claim_id" in dataframe[0] and dataframe[0]["claim_id"] == "c1"
                for dataframe in st.dataframes
                if dataframe
            )
        )

    def test_summary_renders_current_and_baseline_metric_charts(self):
        class FakeColumn:
            def metric(self, *_args, **_kwargs):
                pass

        class FakeExpander:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        class FakeStreamlit:
            def __init__(self):
                self.charts = []

            def columns(self, count):
                return [FakeColumn() for _ in range(count)]

            def error(self, *_args):
                pass

            def warning(self, *_args):
                pass

            def info(self, *_args):
                pass

            def subheader(self, *_args):
                pass

            def altair_chart(self, chart, **kwargs):
                self.charts.append((chart, kwargs))

            def dataframe(self, *_args, **_kwargs):
                pass

            def expander(self, *_args, **_kwargs):
                return FakeExpander()

            def selectbox(self, *_args, **_kwargs):
                return "全部"

            def caption(self, *_args):
                pass

            def markdown(self, *_args):
                pass

            def write(self, *_args):
                pass

        st = FakeStreamlit()
        summary = EvaluationSummary(run_id="current", metric_scores={"faithfulness": 0.8})
        baseline = EvaluationSummary(run_id="baseline", metric_scores={"faithfulness": 0.6})

        _render_summary(st, summary, [], baseline)

        self.assertEqual(len(st.charts), 2)
        current_specification = st.charts[0][0].to_dict()
        baseline_specification = st.charts[1][0].to_dict()
        self.assertEqual(current_specification["layer"][0]["encoding"]["x"]["field"], "score")
        self.assertEqual(
            current_specification["layer"][0]["encoding"]["x"]["scale"]["domain"],
            [0, 1],
        )
        self.assertEqual(
            baseline_specification["layer"][0]["encoding"]["x"]["field"], "score"
        )
        self.assertEqual(
            baseline_specification["layer"][0]["encoding"]["y"]["field"],
            "metric_label",
        )

    def test_current_metric_chart_is_horizontal_and_marks_thresholds(self):
        chart = _build_current_metric_chart(
            [
                {
                    "metric": "faithfulness",
                    "metric_label": "忠实性 (faithfulness)",
                    "score": 0.6,
                    "threshold": 0.75,
                    "meets_threshold": False,
                    "applicable_samples": 4,
                    "scoring_failures": 1,
                }
            ]
        )

        specification = chart.to_dict()
        bar_layer = specification["layer"][0]
        current_text_layer = specification["layer"][1]
        self.assertEqual(bar_layer["encoding"]["x"]["field"], "score")
        self.assertEqual(bar_layer["encoding"]["x"]["scale"]["domain"], [0, 1])
        self.assertEqual(bar_layer["encoding"]["y"]["field"], "metric_label")
        self.assertEqual(current_text_layer["mark"]["type"], "text")
        self.assertEqual(current_text_layer["encoding"]["text"]["field"], "score")
        self.assertEqual(current_text_layer["encoding"]["text"]["format"], ".3f")
        self.assertIn("threshold", str(specification))
        self.assertIn("meets_threshold", str(specification))

    def test_baseline_metric_chart_uses_grouped_horizontal_bars_and_delta_tooltip(self):
        chart = _build_baseline_metric_chart(
            [
                {
                    "metric": "faithfulness",
                    "metric_label": "忠实性 (faithfulness)",
                    "current": 0.8,
                    "baseline": 0.6,
                    "change": 0.2,
                }
            ]
        )

        specification = chart.to_dict()
        comparison_bar_layer = specification["layer"][0]
        comparison_text_layer = specification["layer"][1]
        self.assertEqual(comparison_bar_layer["encoding"]["x"]["field"], "score")
        self.assertEqual(
            comparison_bar_layer["encoding"]["x"]["scale"]["domain"], [0, 1]
        )
        self.assertEqual(
            comparison_bar_layer["encoding"]["y"]["field"], "metric_label"
        )
        self.assertIn("yOffset", comparison_bar_layer["encoding"])
        self.assertEqual(comparison_text_layer["mark"]["type"], "text")
        self.assertEqual(comparison_text_layer["encoding"]["text"]["field"], "score")
        self.assertEqual(comparison_text_layer["encoding"]["text"]["format"], ".3f")
        self.assertEqual(comparison_text_layer["encoding"]["yOffset"]["field"], "run")
        self.assertIn("change", str(specification))

    def test_legacy_summary_without_run_state_still_renders(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            run = Path(temp_dir) / "legacy-run"
            run.mkdir()
            (run / "summary.json").write_text(
                json.dumps({"run_id": "legacy-run"}), encoding="utf-8"
            )

            class FakeStreamlit:
                session_state = {}

                def title(self, *_args, **_kwargs):
                    pass

                def cache_resource(self, function):
                    return function

                def radio(self, *_args, **_kwargs):
                    return "查看历史报告"

                def text_input(self, *_args, **_kwargs):
                    return temp_dir

                def info(self, *_args, **_kwargs):
                    pass

                def selectbox(self, *_args, **_kwargs):
                    return run

            with (
                patch.dict(sys.modules, {"streamlit": FakeStreamlit()}),
                patch("src.ui.evaluation_page._render_summary") as render_summary,
            ):
                render_evaluation_page("system_admin")

            render_summary.assert_called_once()

    def test_history_view_passes_another_completed_run_as_comparison_baseline(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write_summary(root, "20260102T000000Z-current")
            self._write_summary(root, "20260101T000000Z-baseline")
            current = root / "20260102T000000Z-current"
            baseline = root / "20260101T000000Z-baseline"

            class FakeStreamlit:
                session_state = {}

                def title(self, *_args, **_kwargs):
                    pass

                def cache_resource(self, function):
                    return function

                def radio(self, *_args, **_kwargs):
                    return "查看历史报告"

                def text_input(self, *_args, **_kwargs):
                    return temp_dir

                def info(self, *_args, **_kwargs):
                    pass

                def selectbox(self, label, *_args, **_kwargs):
                    return current if label == "运行" else baseline

            with (
                patch.dict(sys.modules, {"streamlit": FakeStreamlit()}),
                patch("src.ui.evaluation_page._render_summary") as render_summary,
            ):
                render_evaluation_page("system_admin")

            self.assertEqual(render_summary.call_args.args[1].run_id, current.name)
            self.assertEqual(render_summary.call_args.args[3].run_id, baseline.name)

    def test_preflight_scoring_reports_missing_ragas_dependency(self):
        with patch("src.ui.evaluation_page.importlib.util.find_spec", return_value=None):
            self.assertIn("uv sync --group eval", preflight_scoring() or "")

    def test_preflight_scoring_reports_invalid_evaluator_configuration(self):
        with (
            patch("src.ui.evaluation_page.importlib.util.find_spec", return_value=object()),
            patch(
                "src.ui.evaluation_page.EvaluationConfig.from_environment",
                side_effect=ValueError("missing embedding configuration"),
            ),
        ):
            self.assertIn("missing embedding configuration", preflight_scoring() or "")

    def test_collection_only_service_factory_does_not_resolve_evaluation_config(self):
        with patch("src.ui.evaluation_page.EvaluationConfig.from_environment") as from_environment:
            service = evaluation_service_factory()()

        self.assertIsNone(service.config)
        from_environment.assert_not_called()

    def test_running_score_enabled_run_stays_running_in_shared_history_controller(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            controller = EvaluationRunController(evaluation_service_factory(), root)
            sample = EvaluationSample(
                id="q1",
                question="Question q1",
                reference_answer="Answer q1",
                kb_name="hardware",
            )
            state = controller.create_online_run(
                root / "dataset.jsonl", root, [sample], score_enabled=True
            )
            RunStateStore(root / state.run_id / "run_state.json").mark_running()
            stop_worker = threading.Event()
            worker = threading.Thread(target=stop_worker.wait)
            worker.start()
            controller._threads[state.run_id] = worker

            try:
                self.assertEqual(
                    controller.load_for_display(state.run_id).status,
                    "running",
                )
            finally:
                stop_worker.set()
                worker.join()


if __name__ == "__main__":
    unittest.main()
