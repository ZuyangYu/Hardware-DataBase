"""Explicit file/project pairing and per-processor work item compilation."""

from __future__ import annotations

from types import SimpleNamespace

from src.services.retrieval_targets import compile_work_items, resolve_turn_targets


def _source(kb_id, name, department="47"):
    return SimpleNamespace(kb_id=kb_id, kb_name=name, department_id=department, role="reference")


def _record(kb_id, record_id, name, *, processor="ragflow", original=None, status="parsed"):
    return SimpleNamespace(
        id=record_id, kb_id=kb_id, document_name=name, original_file_name=original or name,
        processor_kind=processor, status=status, content_hash=f"hash-{record_id}",
        document_id=f"remote-{record_id}", index_revision=1, source_version_id="v1",
        revision="r1", processing_artifact_id=f"parse-{record_id}", processor_version="p1",
    )


class Store:
    def __init__(self, records):
        self.records = records

    def list_documents(self, kb_name, department_id):
        return list(self.records.get(kb_name, []))


SOURCES = [_source(5, "AP230060"), _source(6, "AP230062"), _source(7, "AA250004")]
RECORDS = {
    "AP230060": [
        _record(5, 101, "600605919_EPS_HWREQM.docx"),
        _record(5, 102, "600605919_EPS_HSI.docx"),
        _record(5, 103, "600605919_EPS_ICD.xlsx", processor="spreadsheet_table"),
    ],
    "AP230062": [
        _record(6, 201, "600605918_EPS_HWSolution.docx"),
        _record(6, 202, "600605918_EPS_HSI.docx"),
        _record(6, 203, "600605918_EPS_ICD.xlsx", processor="spreadsheet_table"),
    ],
    "AA250004": [
        _record(7, 301, "600608964_ADAS_HSI_0506_1952_shoulin.wang.docx"),
        _record(7, 302, "600608964_ADAS_产品硬件需求规格说明书ProductHardwareRequirementSpecification.xlsx", processor="spreadsheet_table"),
        _record(7, 303, "600608964_ADAS_ICD_0506_1957_shoulin.wang.xlsx", processor="spreadsheet_table"),
    ],
}
STORE = Store(RECORDS)


def resolve(question, **kwargs):
    return resolve_turn_targets(question, sources=SOURCES, document_store=STORE, **kwargs)


def test_three_project_supply_sentence_pairs_each_file_with_its_project():
    question = "AP230060、AP230062、AA250004 三个项目的供电范围分别是什么？请参考 HWREQM、HWSolution、ADAS HSI。"
    result = resolve(question)

    assert result.resolution == "resolved"
    assert {(target.kb_name, target.record_id) for target in result.targets} == {
        ("AP230060", 101), ("AP230062", 201), ("AA250004", 301),
    }
    assert len(result.targets) == 3


def test_project_clause_disambiguates_same_short_name_in_three_kbs():
    question = "AP230062 的 HSI 中 TJA1043 终端电阻是多少，AP230060 的 HSI 又是多少？"
    result = resolve(question)

    assert {target.record_id for target in result.targets} == {102, 202}


def test_single_short_name_uses_unique_project_record():
    result = resolve("HWREQM 里写的静态电流是多少？")
    assert [target.record_id for target in result.targets] == [101]


def test_explicit_missing_file_is_not_widened_to_the_whole_kb():
    result = resolve("AP230060 的 600605919_MISSING.docx 中供电范围是多少？")
    assert result.resolution == "target_not_found"
    assert result.targets == ()
    assert any(item["reason"] == "explicit_file_not_found" for item in result.diagnostics)


def test_ambiguous_version_same_name_requires_explicit_disambiguation():
    records = {
        "AP230060": [
            _record(5, 101, "same.docx", original="same_v1.docx"),
            _record(5, 102, "same.docx", original="same_v2.docx"),
        ],
    }
    result = resolve_turn_targets("AP230060 的 same.docx 中电压是多少？", sources=[SOURCES[0]], document_store=Store(records))
    assert result.resolution == "target_ambiguous"
    assert result.targets == ()


def test_document_targets_ignore_project_codes_and_supply_words_as_cell_locators():
    result = resolve("分别查 AP230060 的 600605919_EPS_HWREQM.docx 与 AP230062 的 600605918_EPS_HWSolution.docx 的供电范围")
    assert result.resolution == "resolved"
    assert all(len(target.locators) == 1 for target in result.targets)
    assert all(target.locators[0].cell_ref is None for target in result.targets)
    assert all(target.locators[0].sheet_name is None for target in result.targets)


def test_model_query_navigation_names_are_removed_without_losing_terms():
    from src.services.retrieval_targets import sanitize_query_for_targets

    result = resolve("AP230060 的 600605919_EPS_HWREQM.docx 中 KL30 范围和静态电流")
    cleaned = sanitize_query_for_targets(
        "600605919_EPS_HWREQM.docx AP230060 KL30 电压 静态电流", result.targets,
    )
    assert "600605919" not in cleaned
    assert "AP230060" not in cleaned
    assert "KL30" in cleaned and "静态电流" in cleaned


def test_icd_pin_and_sheet_become_record_scoped_locators():
    result = resolve("AP230060 的 600605919_EPS_ICD.xlsx 管脚分配中第2脚的定义是什么？")
    target = result.targets[0]
    assert target.record_id == 103
    assert target.locators[0].sheet_name == "管脚分配"
    assert target.locators[0].pin_value == "2"


def test_same_record_pin_2_and_pin_14_produce_two_locator_branches_not_one_intersection():
    result = resolve("AP230060 的 600605919_EPS_ICD.xlsx 中第2脚和第14脚的电压是多少？")
    target = result.targets[0]
    assert [locator.pin_value for locator in target.locators] == ["2", "14"]


def test_work_items_split_processor_kinds_and_keep_both_spreadsheet_targets():
    result = resolve("AP230060 的 600605919_EPS_ICD.xlsx 和 600605919_EPS_HSI.docx 中第2脚是什么？")
    work_items = compile_work_items(result.targets)

    assert {item.retriever_kind for item in work_items} == {"document", "spreadsheet_semantic"}
    by_kind = {item.retriever_kind: item for item in work_items}
    assert by_kind["spreadsheet_semantic"].contract.allowed_record_ids == (103,)
    assert by_kind["document"].contract.allowed_record_ids == (102,)


def test_work_items_group_multiple_spreadsheet_targets_in_one_kb_into_one_batch():
    result = resolve("AP230060 的 600605919_EPS_ICD.xlsx 第2脚和 AP230060 的 600605919_EPS_ICD.xlsx 第14脚")
    work_items = compile_work_items(result.targets)
    assert len(work_items) == 1
    assert work_items[0].contract.allowed_record_ids == (103,)
    assert [locator.pin_value for locator in work_items[0].locators_by_record[103]] == ["2", "14"]


def test_follow_up_history_hint_rebinds_current_record_and_stamps():
    result = resolve("那静态电流呢", history_hint=[{"kb_id": 5, "record_id": 101}])
    assert result.resolution == "resolved"
    assert result.targets[0].record_id == 101
    assert result.targets[0].source == "history_hint"
    assert result.targets[0].stamps.source_content_hash == "hash-101"
    assert result.targets[0].stamps.processing_artifact_id == "parse-101"


def test_history_hint_for_unavailable_record_does_not_widen_to_the_kb():
    result = resolve("那静态电流呢", history_hint=[{"kb_id": 5, "record_id": 99999}])
    assert result.resolution == "unrestricted_within_kb"
    assert result.targets == ()


def test_project_order_swap_keeps_each_file_with_its_project():
    result = resolve("AA250004 的 ADAS HSI、AP230062 的 HWSolution、AP230060 的 HWREQM 分别给出什么要求？")
    assert result.resolution == "resolved"
    assert {(target.kb_name, target.record_id) for target in result.targets} == {
        ("AA250004", 301), ("AP230062", 201), ("AP230060", 101),
    }


def test_embedded_children_are_added_only_when_question_allows_them():
    parent = _record(5, 101, "parent.docx")
    child = _record(5, 150, "parent [嵌入] sheet.xlsx", processor="spreadsheet_table")
    child.embed_depth = 1
    child.parent_document = '["parent.docx"]'
    store = Store({"AP230060": [parent, child]})

    included = resolve_turn_targets(
        "AP230060 的 parent.docx 包含内嵌表格，列出参数",
        sources=[SOURCES[0]], document_store=store,
    )
    assert {target.record_id for target in included.targets} == {101, 150}
    assert {target.processor_kind for target in included.targets} == {"ragflow", "spreadsheet_table"}

    native_only = resolve_turn_targets(
        "AP230060 的 parent.docx 只使用 Word 原生表格，不要内嵌内容",
        sources=[SOURCES[0]], document_store=store,
    )
    assert {target.record_id for target in native_only.targets} == {101}


def test_history_summary_hint_and_follow_up_heuristic():
    from src.services.retrieval_targets import history_target_hint, looks_like_follow_up

    summary = {"retrieval_targets": {"targets": [
        {"kb_id": 5, "record_id": 101}, {"kb_id": 6, "record_id": 201},
    ]}}
    assert history_target_hint(summary) == [
        {"kb_id": 5, "record_id": 101}, {"kb_id": 6, "record_id": 201},
    ]
    assert looks_like_follow_up("那静态电流呢？")
    assert looks_like_follow_up("继续")
    assert not looks_like_follow_up("AP230060 的供电范围是多少")
