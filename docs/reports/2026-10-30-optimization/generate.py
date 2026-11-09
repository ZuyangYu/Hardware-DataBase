#!/usr/bin/env python3
"""Generate the 2026-10-30 optimization report charts as standalone SVGs.

All numbers come from committed/runtime artifacts:
- /tmp/hdb-question-stress-converged.json
- /tmp/hdb-final-acceptance.json
- /tmp/hdb-full-gap.log
- /tmp/hdb-fe-converged.log
- frontend/test-results/acceptance.json
- git show --numstat for 1ea0838 / 1995eab, git diff --numstat

No before/after value is invented.  Where no baseline exists the chart says
"优化后实测" and plots the measured value only.
"""

from __future__ import annotations

from math import pi
from pathlib import Path
from xml.sax.saxutils import escape

OUT = Path(__file__).resolve().parent
W, H = 1280, 720

FONT = (
    '"Droid Sans Fallback","Noto Sans SC","Source Han Sans SC",'
    '"PingFang SC","Microsoft YaHei",sans-serif'
)
BG = "#f6f7fb"
CARD = "#ffffff"
INK = "#18181a"
MUTED = "#6b7280"
LINE = "#e5e7eb"
BLUE = "#1d4ed8"
BLUE_L = "#dbeafe"
TEAL = "#0f766e"
TEAL_L = "#ccfbf1"
AMBER = "#b45309"
AMBER_L = "#fef3c7"
GREEN = "#15803d"
GREEN_L = "#dcfce7"
RED = "#b91c1c"
GRAY = "#94a3b8"
GRAY_L = "#eef1f6"


def esc(value: object) -> str:
    return escape(str(value), {"'": "&apos;", '"': "&quot;"})


def text(
    x: float,
    y: float,
    value: object,
    *,
    size: float = 14,
    weight: int = 400,
    fill: str = INK,
    anchor: str = "start",
    opacity: float = 1,
    spacing: float = 0,
) -> str:
    return (
        f'<text x="{x:.2f}" y="{y:.2f}" font-size="{size}" font-weight="{weight}" '
        f'fill="{fill}" text-anchor="{anchor}" opacity="{opacity}" '
        f'letter-spacing="{spacing}" dominant-baseline="central">{esc(value)}</text>'
    )


def rect(
    x: float,
    y: float,
    w: float,
    h: float,
    *,
    fill: str = CARD,
    rx: float = 0,
    stroke: str | None = None,
    sw: float = 1,
    opacity: float = 1,
) -> str:
    border = f' stroke="{stroke}" stroke-width="{sw}"' if stroke else ""
    return (
        f'<rect x="{x:.2f}" y="{y:.2f}" width="{w:.2f}" height="{h:.2f}" '
        f'rx="{rx}" fill="{fill}" opacity="{opacity}"{border}/>'
    )


def circle(
    cx: float,
    cy: float,
    r: float,
    *,
    fill: str = "none",
    stroke: str | None = None,
    sw: float = 1,
    opacity: float = 1,
) -> str:
    border = f' stroke="{stroke}" stroke-width="{sw}"' if stroke else ""
    return (
        f'<circle cx="{cx:.2f}" cy="{cy:.2f}" r="{r:.2f}" '
        f'fill="{fill}" opacity="{opacity}"{border}/>'
    )


def line(
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    *,
    stroke: str = LINE,
    sw: float = 1,
    opacity: float = 1,
) -> str:
    return (
        f'<line x1="{x1:.2f}" y1="{y1:.2f}" x2="{x2:.2f}" y2="{y2:.2f}" '
        f'stroke="{stroke}" stroke-width="{sw}" opacity="{opacity}"/>'
    )


def card(x: float, y: float, w: float, h: float, *, fill: str = CARD) -> str:
    return rect(x, y, w, h, fill=fill, rx=18, stroke=LINE, sw=1)


def pill(
    x: float,
    y: float,
    w: float,
    h: float,
    label: str,
    *,
    fill: str = BLUE_L,
    color: str = BLUE,
    size: float = 11,
    weight: int = 600,
) -> str:
    return (
        rect(x, y, w, h, fill=fill, rx=h / 2)
        + text(x + w / 2, y + h / 2, label, size=size, weight=weight, fill=color, anchor="middle")
    )


def check(cx: float, cy: float, color: str = GREEN) -> str:
    return (
        circle(cx, cy, 8.5, fill=GREEN_L)
        + text(cx, cy - 0.5, "✓", size=10.5, weight=700, fill=color, anchor="middle")
    )


def header(title: str, subtitle: str, *, tag: str = "HARDWARE DATABASE · 优化验收 · 2026-10-30") -> str:
    return (
        text(72, 40, tag, size=11, weight=700, fill=BLUE, spacing=1.2)
        + text(72, 82, title, size=31, weight=700, fill=INK)
        + text(72, 112, subtitle, size=13.5, fill=MUTED)
    )


def footer(source: str, page: str) -> str:
    return (
        line(72, 668, 1208, 668, stroke=LINE, sw=1)
        + text(72, 691, source, size=10, fill=GRAY)
        + text(1208, 691, page, size=10, fill=GRAY, anchor="end")
    )


def svg(title: str, desc: str, body: str) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
        f'viewBox="0 0 {W} {H}" role="img" aria-labelledby="chart-title chart-desc">'
        "<title id=\"chart-title\">"
        f"{esc(title)}"
        "</title><desc id=\"chart-desc\">"
        f"{esc(desc)}"
        "</desc>"
        "<style>"
        f"text{{font-family:{FONT};font-variant-numeric:tabular-nums;}}"
        "</style>"
        f'<rect width="{W}" height="{H}" fill="{BG}"/>'
        f"{body}</svg>\n"
    )


def kpi(
    x: float,
    y: float,
    w: float,
    h: float,
    value: str,
    label: str,
    sub: str,
    *,
    color: str = BLUE,
) -> str:
    return (
        card(x, y, w, h)
        + rect(x + 1, y + 18, 4, h - 36, fill=color, rx=2)
        + text(x + 22, y + 30, value, size=28, weight=700, fill=color)
        + text(x + 22, y + 59, label, size=12.5, weight=600, fill=INK)
        + text(x + 22, y + 78, sub, size=10.5, fill=MUTED)
    )


# --------------------------------------------------------------------------
# 01 · Retrieval funnel
# --------------------------------------------------------------------------
def chart_01() -> str:
    body = header(
        "检索链路：89.4% 的候选条目没有进入正式证据账本",
        "10 题 ADAS 压测 · AAA 知识库 · 并发 4 · 优化后实测",
    )
    body += kpi(72, 140, 272, 96, "1,209", "候选条目", "tool_result.candidate_count 累计", color=BLUE)
    body += kpi(360, 140, 272, 96, "66", "evidence_read 调用", "模型按需精读候选", color=TEAL)
    body += kpi(648, 140, 272, 96, "128", "正式证据", "去重后进入引用账本", color=GREEN)
    body += kpi(936, 140, 272, 96, "10 / 10", "回答带引用", "answers_with_citations = 10", color=AMBER)

    body += card(72, 260, 1136, 306)
    body += text(104, 298, "候选条目去向", size=15, weight=700, fill=INK)
    body += text(104, 320, "搜索命中先进入候选池；只有被 evidence_read 选中的正文才获得引用编号。", size=11, fill=MUTED)

    bar_x, bar_y, bar_w, bar_h = 104, 350, 1072, 68
    blue_w = bar_w * 128 / 1209
    gray_w = bar_w - blue_w
    body += rect(bar_x, bar_y, bar_w, bar_h, fill=GRAY_L, rx=12)
    body += rect(bar_x, bar_y, blue_w, bar_h, fill=BLUE, rx=12)
    body += text(bar_x + blue_w / 2, bar_y + bar_h / 2, "10.6%", size=15, weight=700, fill="#ffffff", anchor="middle")
    body += text(
        bar_x + blue_w + gray_w / 2,
        bar_y + bar_h / 2,
        "差额 1,081 条 · 89.4%",
        size=15,
        weight=700,
        fill=INK,
        anchor="middle",
    )
    body += text(bar_x, bar_y + bar_h + 20, "正式证据 128 条", size=11, weight=600, fill=BLUE)
    body += text(bar_x + bar_w, bar_y + bar_h + 20, "候选与证据差额 1,081 条", size=11, weight=600, fill=MUTED, anchor="end")

    stats = [
        ("120.9 条", "平均每题候选"),
        ("18.3 条", "平均每次精读带入候选"),
        ("1.9 条", "平均每次精读提升证据"),
    ]
    for index, (value, label) in enumerate(stats):
        x = 104 + index * 348
        body += text(x, 474, value, size=22, weight=700, fill=INK)
        body += text(x, 500, label, size=11, fill=MUTED)

    body += text(104, 540, "口径：候选为各工具 candidate_count 的累计值（含重复命中）；10.6% 是候选条目与正式证据的聚合比例，不是严格集合差。", size=10, fill=GRAY)

    body += card(72, 586, 1136, 56, fill="#eef4ff")
    body += text(
        104,
        614,
        "效果：候选模式把“搜到即引用”改为“精读后才引用”，候选条目与正式证据的比例为 10.6%。",
        size=12.5,
        weight=600,
        fill=BLUE,
    )
    body += footer("数据：/tmp/hdb-question-stress-converged.json · 2026-10-30 12:02–12:16 · 10 题 / 0 失败", "01 / 07")
    return svg("检索链路候选漏斗", "1209 条候选，66 次 evidence_read，128 条正式证据，89.4% 候选未占用证据预算。", body)


# --------------------------------------------------------------------------
# 02 · Per-question retrieval quality
# --------------------------------------------------------------------------
def chart_02() -> str:
    candidates = [34, 112, 185, 160, 190, 108, 88, 68, 191, 73]
    evidence = [11, 11, 16, 19, 16, 12, 6, 11, 19, 7]
    citations = [9, 8, 13, 17, 13, 12, 6, 10, 17, 7]

    body = header(
        "逐题检索质量：10/10 有引用，平均 12.8 条证据",
        "候选 → 精读 → 正式证据 → 引用标记 · 10 题 ADAS 压测",
    )

    # Top: candidates
    body += card(72, 136, 1136, 246)
    body += text(104, 170, "候选条目 / 题", size=15, weight=700, fill=INK)
    body += rect(930, 164, 10, 10, fill=GRAY, rx=2)
    body += text(948, 170, "候选条目", size=10.5, fill=MUTED)
    plot_x, plot_y, plot_w, plot_h = 120, 198, 1040, 140
    max_c = 200
    group_w = plot_w / 10
    for idx, value in enumerate(candidates):
        cx = plot_x + group_w * idx + group_w / 2
        h = value / max_c * plot_h
        body += rect(cx - 23, plot_y + plot_h - h, 46, h, fill=GRAY, rx=5)
        body += text(cx, plot_y + plot_h - h - 10, str(value), size=10.5, weight=600, fill=MUTED, anchor="middle")
        body += text(cx, plot_y + plot_h + 18, f"Q{idx + 1}", size=10.5, fill=GRAY, anchor="middle")
    body += line(120, 338, 1160, 338, stroke=LINE)

    # Bottom: evidence + citations
    body += card(72, 398, 1136, 232)
    body += text(104, 432, "正式证据 / 引用标记 / 题", size=15, weight=700, fill=INK)
    body += rect(930, 426, 10, 10, fill=BLUE, rx=2)
    body += text(948, 432, "正式证据", size=10.5, fill=MUTED)
    body += rect(1030, 426, 10, 10, fill=TEAL, rx=2)
    body += text(1048, 432, "引用标记", size=10.5, fill=MUTED)

    plot_x2, plot_y2, plot_w2, plot_h2 = 120, 462, 1040, 120
    max_q = 20
    group_w2 = plot_w2 / 10
    for idx, (ev, ct) in enumerate(zip(evidence, citations)):
        center = plot_x2 + group_w2 * idx + group_w2 / 2
        h_ev = ev / max_q * plot_h2
        h_ct = ct / max_q * plot_h2
        body += rect(center - 25, plot_y2 + plot_h2 - h_ev, 21, h_ev, fill=BLUE, rx=4)
        body += rect(center + 4, plot_y2 + plot_h2 - h_ct, 21, h_ct, fill=TEAL, rx=4)
        body += text(center - 14.5, plot_y2 + plot_h2 - h_ev - 8, str(ev), size=9.5, weight=600, fill=BLUE, anchor="middle")
        body += text(center + 14.5, plot_y2 + plot_h2 - h_ct - 8, str(ct), size=9.5, weight=600, fill=TEAL, anchor="middle")
        body += text(center, plot_y2 + plot_h2 + 17, f"Q{idx + 1}", size=10.5, fill=GRAY, anchor="middle")
    body += line(120, 590, 1160, 590, stroke=LINE)

    body += pill(72, 636, 150, 26, "平均证据 12.8", fill=BLUE_L, color=BLUE)
    body += pill(234, 636, 150, 26, "平均引用 11.2", fill=TEAL_L, color=TEAL)
    body += pill(396, 636, 166, 26, "平均精读 6.6 次", fill=GREEN_L, color=GREEN)
    body += text(1208, 649, "10 题全部完成 · 0 失败 · 0 预算耗尽", size=10.5, fill=MUTED, anchor="end")
    body += footer("数据：/tmp/hdb-question-stress-converged.json · 题目见 scripts/acceptance/adas_stress_questions.json", "02 / 07")
    return svg(
        "逐题检索质量",
        "10 道题的候选条目、正式证据、引用标记分布；10 题全部有引用。",
        body,
    )


# --------------------------------------------------------------------------
# 03 · Acceptance matrix
# --------------------------------------------------------------------------
def chart_03() -> str:
    body = header(
        "端到端验收：16/16 项全部通过",
        "候选检索、上下文清单、事件契约、并行配对、前端降噪",
    )
    body += rect(1010, 48, 198, 72, fill=BLUE, rx=16)
    body += text(1109, 76, "16 / 16", size=30, weight=700, fill="#ffffff", anchor="middle")
    body += text(1109, 102, "验收通过 · 0 失败", size=10.5, fill="#dbeafe", anchor="middle")

    columns = [
        (
            72,
            "检索与证据",
            "7 / 7",
            [
                ("知识库可见", "kb_visible"),
                ("本轮正常完成", "turn_completed"),
                ("候选模式生效", "candidate_mode_active"),
                ("evidence_read 被调用", "evidence_read_used"),
                ("每个工具结果带候选数", "candidate_count_on_every_tool_result"),
                ("回答包含引用", "answer_has_citations"),
                ("正式证据在预算内", "formal_evidence_within_budget"),
            ],
            "候选 188 · 证据 13 · 回答 2,888 字",
        ),
        (
            360,
            "上下文与回放",
            "3 / 3",
            [
                ("context_manifest 存在", "context_manifest_present"),
                ("刷新后候选数可回放", "candidate_count_replayed_after_refresh"),
                ("压缩事件字段完整", "compaction_event_typed"),
            ],
            "42 步轨迹回放 · 压缩事件 typed",
        ),
        (
            648,
            "事件与并发",
            "4 / 4",
            [
                ("工具事件全部 typed", "tool_events_typed"),
                ("并行 call_id 唯一配对", "parallel_tool_ids_unique_and_paired"),
                ("并行调用真实重叠", "parallel_tool_calls_overlap"),
                ("并行结果全部 typed", "parallel_tool_results_typed"),
            ],
            "2 / 2 call_id 配对 · 重叠成立",
        ),
        (
            936,
            "前端",
            "2 / 2",
            [
                ("预算/校验/计划卡片已移除", "frontend_meta_cards_removed"),
                ("构建标记匹配", "frontend_build_marker_updated"),
            ],
            "0 个禁用文案 · build 已更新",
        ),
    ]

    for x, title, count, rows, summary in columns:
        body += card(x, 140, 272, 470)
        body += text(x + 20, 174, title, size=14.5, weight=700, fill=INK)
        body += pill(x + 198, 160, 54, 24, count, fill=GREEN_L, color=GREEN, size=10.5)
        y = 218
        for label, code in rows:
            body += check(x + 28, y + 2)
            body += text(x + 44, y - 3, label, size=11.2, weight=600, fill=INK)
            body += text(x + 44, y + 12, code, size=8.8, fill=GRAY)
            y += 42
        body += line(x + 20, 520, x + 252, 520, stroke=LINE)
        body += text(x + 20, 542, summary, size=9.5, fill=MUTED)

    body += card(72, 628, 1136, 28, fill="#eef4ff")
    body += text(
        104,
        642,
        "口径：/tmp/hdb-final-acceptance.json 的 16 项检查全部 passed；并行检查使用同一回合的 2 个并行调用。",
        size=10,
        fill=BLUE,
    )
    body += footer("数据：/tmp/hdb-final-acceptance.json · 2026-10-30 12:29", "03 / 07")
    return svg("端到端验收矩阵", "16 项端到端检查全部通过，覆盖检索、上下文、事件并发和前端。", body)


# --------------------------------------------------------------------------
# 04 · Test coverage
# --------------------------------------------------------------------------
def chart_04() -> str:
    body = header(
        "测试覆盖：2,514 项通过，0 失败",
        "后端全量 + 前端单测 + E2E + 本次相关回归",
    )

    body += card(72, 136, 700, 496)
    body += text(104, 170, "全量测试", size=15, weight=700, fill=INK)

    rows = [
        ("后端 pytest", "2,280 通过 / 189 跳过", 2280, 189, 644.6, "92.3%"),
        ("前端单测 Vitest", "161 通过 / 0 跳过", 161, 0, 4.5, "100%"),
        ("前端 E2E Playwright", "73 通过 / 9 跳过", 73, 9, 121.1, "89.0%"),
    ]
    y = 214
    for label, detail, passed, skipped, duration, rate in rows:
        body += text(104, y, label, size=13, weight=700, fill=INK)
        body += text(104, y + 18, detail, size=10.5, fill=MUTED)
        body += pill(600, y - 12, 68, 22, "0 失败", fill=GREEN_L, color=GREEN, size=9.5)
        total = passed + skipped
        bar_x, bar_y, bar_w, bar_h = 104, y + 34, 636, 18
        pass_w = bar_w * passed / total if total else bar_w
        body += rect(bar_x, bar_y, bar_w, bar_h, fill=GRAY_L, rx=9)
        body += rect(bar_x, bar_y, pass_w, bar_h, fill=BLUE, rx=9)
        body += text(104, y + 72, f"通过率 {rate} · 用时 {duration:g}s", size=10, fill=GRAY)
        y += 104

    body += line(104, 540, 740, 540, stroke=LINE)
    body += text(104, 562, "跳过项为环境/外部依赖条件不满足，不计为失败。", size=10, fill=MUTED)
    body += text(104, 586, "后端全量覆盖 2,469 项（2,280 通过 + 189 跳过）。", size=10, fill=MUTED)

    body += card(788, 136, 420, 496)
    body += text(820, 170, "本次相关回归（项）", size=15, weight=700, fill=INK)
    body += text(820, 226, "74", size=44, weight=700, fill=BLUE)
    body += text(880, 226, "项 · 5.95s", size=13, fill=MUTED)
    body += text(820, 252, "全部通过；为后端全量子集，不重复计入总数。", size=10, fill=MUTED)

    areas = [("检索/候选", 24), ("上下文", 9), ("循环", 5), ("事件", 13), ("运行时", 23)]
    plot_x, plot_y, plot_w, plot_h = 820, 300, 360, 190
    max_v = 24
    slot = plot_w / len(areas)
    for idx, (label, value) in enumerate(areas):
        cx = plot_x + slot * idx + slot / 2
        h = value / max_v * plot_h
        body += rect(cx - 22, plot_y + plot_h - h, 44, h, fill=BLUE, rx=5)
        body += text(cx, plot_y + plot_h - h - 10, str(value), size=12, weight=700, fill=INK, anchor="middle")
        body += text(cx, plot_y + plot_h + 18, label, size=9.8, fill=MUTED, anchor="middle")
    body += line(820, 548, 1176, 548, stroke=LINE)
    body += text(820, 570, "用例：候选检索、kb_grep、上下文、会话召回、循环推进、事件契约、运行时元数据。", size=9.2, fill=GRAY)
    body += text(820, 590, "命令：pytest -q tests/test_candidate_retrieval.py … test_agent_turn_meta.py", size=9.2, fill=GRAY)

    body += footer(
        "数据：/tmp/hdb-full-gap.log · /tmp/hdb-fe-converged.log · frontend/test-results/acceptance.json · 本地 targeted pytest 2026-10-30",
        "04 / 07",
    )
    return svg("测试覆盖与通过率", "后端 2280 通过、前端单测 161 通过、E2E 73 通过，相关回归 74 项全部通过。", body)


# --------------------------------------------------------------------------
# 05 · Frontend noise reduction
# --------------------------------------------------------------------------
def chart_05() -> str:
    body = header(
        "聊天区降噪：3 个元信息面板 → 0，代码少 417 行",
        "处理过程改为按 call_id 配对的扁平时间线",
    )

    body += card(72, 140, 556, 150)
    body += text(104, 190, "3 → 0", size=48, weight=700, fill=BLUE)
    body += text(104, 232, "元信息面板", size=16, weight=700, fill=INK)
    body += text(104, 256, "执行计划 / 预算 / 系统校验", size=11.5, fill=MUTED)

    body += card(652, 140, 556, 150)
    body += text(684, 190, "6 → 0", size=48, weight=700, fill=TEAL)
    body += text(684, 232, "禁用文案", size=16, weight=700, fill=INK)
    body += text(684, 256, "预算耗尽 / 部分交付 / 缩小范围 等", size=11.5, fill=MUTED)

    body += card(72, 320, 1136, 250)
    body += text(104, 354, "前端代码行数（before → after）", size=15, weight=700, fill=INK)
    max_w = 900
    files = [
        ("MessageBubble.tsx", 903, 710, 104, 390),
        ("useKbChat.ts", 1499, 1275, 104, 486),
    ]
    for label, before, after, lx, ly in files:
        body += text(lx, ly, label, size=12.5, weight=700, fill=INK)
        body += text(lx, ly + 20, f"{before:,} 行", size=10.5, fill=MUTED)
        before_w = before / 1500 * max_w
        after_w = after / 1500 * max_w
        body += rect(260, ly - 8, before_w, 20, fill=GRAY_L, rx=5)
        body += rect(260, ly + 20, after_w, 20, fill=BLUE, rx=5)
        body += text(260 + before_w + 14, ly + 2, f"{before:,}", size=10.5, fill=MUTED)
        body += text(260 + after_w + 14, ly + 30, f"{after:,}", size=10.5, weight=700, fill=BLUE)
        delta = after - before
        pct = delta / before * 100
        body += text(1208, ly + 16, f"{delta:+,} 行 · {pct:+.1f}%", size=11.5, weight=700, fill=GREEN, anchor="end")
    body += line(104, 536, 1176, 536, stroke=LINE)
    body += text(104, 556, "合计减少 417 行；同时保留证据、记忆、附件导出等业务区块。", size=10.5, fill=MUTED)

    body += pill(72, 604, 190, 32, "42 步刷新回放", fill=BLUE_L, color=BLUE)
    body += pill(274, 604, 190, 32, "2 / 2 并行配对", fill=TEAL_L, color=TEAL)
    body += pill(476, 604, 190, 32, "0 个禁用文案", fill=GREEN_L, color=GREEN)
    body += text(1208, 620, "面板与文案检查：frontend_meta_cards_removed = passed", size=10.5, fill=MUTED, anchor="end")

    body += footer("数据：git diff HEAD · /tmp/hdb-final-acceptance.json · 代码行数为 wc -l 统计", "05 / 07")
    return svg(
        "聊天区降噪前后对比",
        "移除执行计划、预算、系统校验三个面板和六条禁用文案，前端代码减少 417 行。",
        body,
    )


# --------------------------------------------------------------------------
# 06 · Architecture slimming
# --------------------------------------------------------------------------
def donut_segment(cx: float, cy: float, r: float, sw: float, start: float, end: float, color: str) -> str:
    circumference = 2 * pi * r
    length = (end - start) * circumference
    gap = circumference - length
    offset = -start * circumference
    return (
        f'<circle cx="{cx:.2f}" cy="{cy:.2f}" r="{r:.2f}" fill="none" '
        f'stroke="{color}" stroke-width="{sw}" '
        f'stroke-dasharray="{length:.2f} {gap:.2f}" '
        f'stroke-dashoffset="{offset:.2f}" '
        f'transform="rotate(-90 {cx:.2f} {cy:.2f})"/>'
    )


def chart_06() -> str:
    body = header(
        "架构瘦身：本周 git 删除 136,401 行",
        "两个已提交批次 + 当前工作区（仅统计 tracked diff）",
    )

    total_del = 126164 + 6954 + 3283
    body += card(72, 136, 520, 496)
    body += text(104, 170, "删除行构成", size=15, weight=700, fill=INK)

    cx, cy, r, sw = 332, 354, 128, 44
    body += circle(cx, cy, r, stroke=GRAY_L, sw=sw)
    fractions = [
        (0.0, 126164 / total_del, BLUE),
        (126164 / total_del, (126164 + 6954) / total_del, TEAL),
        ((126164 + 6954) / total_del, 1.0, AMBER),
    ]
    for start, end, color in fractions:
        body += donut_segment(cx, cy, r, sw, start, end, color)
    body += text(cx, cy - 8, f"{total_del:,}", size=28, weight=700, fill=INK, anchor="middle")
    body += text(cx, cy + 20, "行删除", size=12, fill=MUTED, anchor="middle")

    legend = [
        (BLUE, "文档生成（含测试/文档）", "126,164", "92.5%"),
        (TEAL, "旧比较/任务编排", "6,954", "5.1%"),
        (AMBER, "当前候选/上下文/前端", "3,283", "2.4%"),
    ]
    y = 532
    for color, label, value, pct in legend:
        body += rect(104, y - 6, 10, 10, fill=color, rx=2)
        body += text(122, y, label, size=10.5, fill=INK)
        body += text(400, y, value, size=10.5, weight=700, fill=INK, anchor="end")
        body += text(456, y, pct, size=10, fill=MUTED, anchor="end")
        y += 26

    body += card(620, 136, 588, 496)
    body += text(652, 170, "批次明细", size=15, weight=700, fill=INK)
    headers = ["批次", "文件", "+ 行", "− 行"]
    xs = [652, 930, 1020, 1110]
    for x, label in zip(xs, headers):
        body += text(x, 206, label, size=10, weight=700, fill=MUTED)
    body += line(652, 220, 1176, 220, stroke=LINE)

    rows = [
        ("10/27 旧比较/任务栈", "69", "2,984", "6,954"),
        ("10/28 文档生成", "442", "1,166", "126,164"),
        ("10/30 当前工作区", "43 + 20 新", "1,723", "3,283"),
        ("合计", "—", "5,873", "136,401"),
    ]
    y = 250
    for index, (name, files, added, deleted) in enumerate(rows):
        weight = 700 if index == len(rows) - 1 else 400
        body += text(xs[0], y, name, size=10.8, weight=weight, fill=INK)
        body += text(xs[1], y, files, size=10.8, weight=weight, fill=INK)
        body += text(xs[2], y, added, size=10.8, weight=weight, fill=GREEN)
        body += text(xs[3], y, deleted, size=10.8, weight=weight, fill=RED)
        body += line(652, y + 20, 1176, y + 20, stroke=LINE, opacity=0.7)
        y += 48

    body += pill(652, 452, 166, 30, "55 个 API 端点", fill=BLUE_L, color=BLUE, size=10)
    body += pill(830, 452, 166, 30, "7 个 task_* 模块", fill=TEAL_L, color=TEAL, size=10)
    body += pill(1008, 452, 166, 30, "442 个文件", fill=AMBER_L, color=AMBER, size=10)

    body += text(652, 510, "当前工作区新增文件内容未计入 + 行；合计仅统计 tracked diff。", size=9.5, fill=GRAY)
    body += text(652, 532, "净删除约 13 万行（tracked）；新增行包含迁移与兼容代码。", size=9.5, fill=GRAY)
    body += text(652, 566, "文档生成子系统仍保留被复用的 ooxml 与附件预览能力。", size=9.5, fill=GRAY)
    body += text(652, 588, "行数含测试、文档与迁移文件，不等同于生产代码行数。", size=9.5, fill=GRAY)

    body += footer("数据：git show --numstat 1ea0838 / 1995eab · git diff --numstat · 2026-10-30", "06 / 07")
    return svg(
        "架构瘦身统计",
        "本周 git 删除 136,401 行，其中文档生成子系统占 92.5%，旧比较/任务编排占 5.1%。",
        body,
    )


# --------------------------------------------------------------------------
# 07 · Context governance
# --------------------------------------------------------------------------
def chart_07() -> str:
    body = header(
        "上下文治理：32k 模型不再继承 64k 窗口",
        "模型窗口解析单测 + 10 题压测的上下文/预算稳定性",
    )

    body += card(72, 136, 700, 496)
    body += text(104, 170, "模型窗口解析（配置值 vs 生效值）", size=15, weight=700, fill=INK)
    body += rect(500, 164, 10, 10, fill=GRAY_L, rx=2)
    body += text(518, 170, "配置上限", size=10, fill=MUTED)
    body += rect(600, 164, 10, 10, fill=BLUE, rx=2)
    body += text(618, 170, "生效窗口", size=10, fill=MUTED)

    cases = [
        ("Qwen2.5 32B", "ollama · qwen2.5:32b", 65536, 32768, "capped 50%"),
        ("DeepSeek", "custom · deepseek-chat", 32768, 32768, "保持配置"),
        ("未知模型", "custom · private-model", 65536, 65536, "保持配置"),
        ("关闭", "max_input_tokens = 0", 0, 0, "压缩关闭"),
    ]
    bar_x, max_w, scale = 300, 380, 65536
    y = 220
    for label, sub, configured, effective, note in cases:
        body += text(104, y, label, size=12.5, weight=700, fill=INK)
        body += text(104, y + 17, sub, size=9.5, fill=GRAY)
        cw = configured / scale * max_w
        ew = effective / scale * max_w
        body += rect(bar_x, y - 4, max(cw, 1), 14, fill=GRAY_L, rx=4)
        body += rect(bar_x, y + 16, max(ew, 1), 14, fill=BLUE, rx=4)
        if configured == 0:
            body += text(bar_x + 8, y + 2, "0", size=9.5, fill=MUTED)
            body += text(bar_x + 8, y + 22, "0（关闭）", size=9.5, fill=BLUE)
        else:
            body += text(bar_x + cw + 8, y + 2, f"{configured:,}", size=9.5, fill=MUTED)
            body += text(bar_x + ew + 8, y + 22, f"{effective:,}", size=9.5, weight=700, fill=BLUE)
        body += text(bar_x, y + 43, note, size=9.5, weight=600, fill=AMBER if "capped" in note else MUTED)
        y += 82

    body += line(104, 552, 740, 552, stroke=LINE)
    body += text(104, 576, "Qwen2.5 的 64k 全局默认被压回 32,768，避免压缩触发晚于模型真实窗口。", size=10.2, fill=MUTED)
    body += text(104, 598, "未知模型保持配置值；配置为 0 时仍关闭主动压缩。", size=10.2, fill=MUTED)

    body += card(788, 136, 420, 496)
    body += text(820, 170, "10 题压测稳定性", size=15, weight=700, fill=INK)
    stability = [
        ("完成", "10 / 10", GREEN),
        ("失败", "0", MUTED),
        ("预算耗尽", "0", MUTED),
        ("递归上限", "0", MUTED),
        ("部分交付", "0", MUTED),
        ("上下文压缩", "0", MUTED),
        ("上下文压力", "0", MUTED),
    ]
    y = 220
    for label, value, color in stability:
        body += text(820, y, label, size=12, fill=INK)
        body += text(1176, y, value, size=18 if label == "完成" else 15, weight=700, fill=color, anchor="end")
        body += line(820, y + 20, 1176, y + 20, stroke=LINE, opacity=0.7)
        y += 46
    body += text(820, 574, "0 次压缩表示本轮未触发窗口压缩，不是异常。", size=9.5, fill=GRAY)
    body += text(820, 596, "compaction_event_typed 验收通过。", size=9.5, fill=GRAY)

    body += footer("数据：tests/test_context_budget.py · /tmp/hdb-question-stress-converged.json · /tmp/hdb-final-acceptance.json", "07 / 07")
    return svg(
        "上下文治理",
        "Qwen2.5 的 64k 配置被压回 32k；10 题压测 0 失败、0 预算耗尽、0 上下文异常。",
        body,
    )


def main() -> None:
    charts = {
        "01-retrieval-funnel.svg": chart_01,
        "02-retrieval-quality.svg": chart_02,
        "03-acceptance-matrix.svg": chart_03,
        "04-test-coverage.svg": chart_04,
        "05-frontend-noise.svg": chart_05,
        "06-code-slimming.svg": chart_06,
        "07-context-governance.svg": chart_07,
    }
    for name, builder in charts.items():
        path = OUT / name
        path.write_text(builder(), encoding="utf-8")
        print(f"wrote {path}")


if __name__ == "__main__":
    main()
