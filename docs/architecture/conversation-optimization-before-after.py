#!/usr/bin/env python3
"""Generate a before/after conversation-flow diagram for the 2026-10 optimization."""
from __future__ import annotations

import html
import os

from PIL import Image, ImageDraw, ImageFont

OUT_DIR = os.path.dirname(os.path.abspath(__file__))
DRAWIO_PATH = os.path.join(OUT_DIR, "conversation-optimization-before-after.drawio")
PREVIEW_PATH = os.path.join(OUT_DIR, "conversation-optimization-before-after.preview.png")

W, H = 2080, 1740
LEFT_X, LEFT_W = 60, 780
MID_X, MID_W = 880, 320
RIGHT_X, RIGHT_W = 1240, 780
HEADER_Y, HEADER_H = 130, 56
ROW_Y0, ROW_H, ROW_PITCH = 220, 120, 144
CIRCLE = 28

COLORS = {
    "before_header": "#475569",
    "before_fill": "#F8FAFC",
    "before_stroke": "#94A3B8",
    "before_circle": "#64748B",
    "before_arrow": "#94A3B8",
    "after_header": "#1D4ED8",
    "after_fill": "#EFF6FF",
    "after_stroke": "#2563EB",
    "after_circle": "#2563EB",
    "after_arrow": "#2563EB",
    "mid_header": "#B45309",
    "mid_fill": "#FFFBEB",
    "mid_stroke": "#D97706",
    "mid_arrow": "#D97706",
    "text": "#0F172A",
    "body": "#334155",
}

ROWS = [
    {
        "n": 1,
        "title": "提问与 turn 创建",
        "before": [
            "POST /conversations/{id}/turns → 落库 user 消息",
            "worker claim turn · 租约 + fencing_token",
            "SSE 订阅 /turns/{id}/events",
        ],
        "after": [
            "POST /conversations/{id}/turns → 落库 user 消息",
            "worker claim turn · 租约 + fencing_token",
            "SSE 订阅 /turns/{id}/events",
        ],
        "change": ("统一入口", "turn 状态机与 SSE 传输不变"),
    },
    {
        "n": 2,
        "title": "计划与循环入口",
        "before": [
            "deep 模式先 write_todos 建 3-7 步计划",
            "plan_created / plan_updated 下发前端",
            "前端渲染「执行计划」卡片",
        ],
        "after": [
            "计划模式已移除，不再提供 write_todos",
            "直接进入 deepagents ReAct 工具循环",
            "模型自主决定工具、顺序与跳数",
        ],
        "change": ("计划模式移除", "少一次规划跳数，少一类 UI 卡片"),
    },
    {
        "n": 3,
        "title": "检索命中直接准入",
        "before": [
            "document_search / circuit_search / 表格 / 对话返回 Evidence[]",
            "add_evidence() 自动进入正式证据账本",
            "宽泛命中先占用证据预算",
        ],
        "after": [
            "全部命中先进入 candidate_evidence",
            "返回 id / 来源 / 定位 / score / ≤180 字摘要",
            "候选上限 60；不可引用、不占正式预算",
        ],
        "change": ("候选 / 证据分离", "命中不再自动获得 [n]"),
    },
    {
        "n": 4,
        "title": "命中即引用",
        "before": [
            "命中即分配引用编号 [n]",
            "后续更相关的证据可能因预算被裁掉",
            "模型没有二次选择证据的机会",
        ],
        "after": [
            "模型调用 evidence_read(candidate_ids)",
            "选中的候选才进入正式证据账本并分配 [n]",
            "已提升候选移出候选池；缺口时提示 kb_grep",
        ],
        "change": ("按需提升", "只对真正引用的正文付费"),
    },
    {
        "n": 5,
        "title": "单轮会话上下文",
        "before": [
            "只带当前 turn 的查询 / 证据 / 历史消息",
            "上一轮证据不主动复用",
            "追问往往需要重新检索",
        ],
        "after": [
            "预取最近 3 轮已读证据到候选池",
            "上一轮 answer_gaps 作为遗留缺口注入",
            "跨轮复用；正式引用仍在当轮重新登记",
        ],
        "change": ("跨轮续接", "追问不必从零检索"),
    },
    {
        "n": 6,
        "title": "工具事件配对",
        "before": [
            "tool_started / tool_result 按工具名 + 序号启发式配对",
            "并行调用 / 重试可能挂错轨迹项",
            "前端按工具名做兜底",
        ],
        "after": [
            "事件带 schema_version / item_id / call_id",
            "agentEvents.ts 按 call_id 精确配对",
            "tool_result 展示候选 / 证据 / 来源 / 空结果原因",
        ],
        "change": ("稳定关联", "并行与重试不再依赖工具名"),
    },
    {
        "n": 7,
        "title": "上下文压缩",
        "before": [
            "统一 max_input_tokens；压缩靠推断",
            "context_compacted 混合证据压缩 / 窗口压缩",
            "前端用预算卡展示上下文信息",
        ],
        "after": [
            "按模型真实窗口解析，避免压缩过晚",
            "读取 _summarization_event 发真实 context_compacted",
            "cutoff / tokens / hash / offload；manifest 只落长度与哈希",
        ],
        "change": ("可审计压缩", "压缩不再靠猜"),
    },
    {
        "n": 8,
        "title": "循环预算与收尾",
        "before": [
            "较低 recursion limit（deep rounds×12 / fast rounds×6，cap 72）",
            "到限即部分交付",
            "前端「预算用完 / 继续 / 缩小范围」",
        ],
        "after": [
            "deep max(48, rounds×16)；fast max(24, rounds×8)；cap 120",
            "每次工具调用检查新增证据 / 候选 / 来源",
            "连续无进展才 no_progress 收尾；有进展可继续",
        ],
        "change": ("进度感知", "不再固定步数硬砍"),
    },
    {
        "n": 9,
        "title": "前端呈现",
        "before": [
            "执行计划 / 预算 / 系统校验卡片 + 执行轨迹",
            "信息密度高，过程噪声大",
            "用户需要分辨哪些是过程、哪些是结论",
        ],
        "after": [
            "只保留「处理过程」：候选数 / 证据数 / 来源 / 空结果原因",
            "无计划、预算、校验卡片",
            "降级提示保留；结论优先",
        ],
        "change": ("安静 UI", "过程可见，结论优先"),
    },
    {
        "n": 10,
        "title": "完成与落账",
        "before": [
            "done 权威回答 + citations + footer",
            "turn / evidence / query trace 落库",
            "SSE 断点续传；刷新不丢 turn",
        ],
        "after": [
            "done 权威回答 + citations + footer",
            "turn / evidence / query trace 落库",
            "SSE 断点续传；刷新不丢 turn",
        ],
        "change": ("结果落账不变", "可靠性边界保持"),
    },
]


def esc(v: str) -> str:
    return html.escape(v, quote=True)


cells: list[str] = []


def add_vertex(cid, value, style, x, y, w, h):
    cells.append(
        f'<mxCell id="{cid}" value="{esc(value)}" style="{style}" vertex="1" parent="1">'
        f'<mxGeometry x="{x}" y="{y}" width="{w}" height="{h}" as="geometry"/></mxCell>'
    )


def add_text(cid, value, x, y, w, h, size=12, color="#0F172A", bold=False, align="left", valign="middle"):
    style = (
        f"text;html=1;strokeColor=none;fillColor=none;align={align};verticalAlign={valign};"
        f"whiteSpace=wrap;fontSize={size};fontColor={color};"
    )
    if bold:
        style += "fontStyle=1;"
    add_vertex(cid, value, style, x, y, w, h)


def add_lane_header(cid, title, x, fill):
    add_vertex(
        cid, "",
        f"rounded=1;arcSize=6;whiteSpace=wrap;html=1;fillColor={fill};strokeColor={fill};strokeWidth=1;",
        x, HEADER_Y, LEFT_W if x != MID_X else MID_W, HEADER_H,
    )
    add_text(f"{cid}_t", title, x + 16, HEADER_Y + 8,
             (LEFT_W if x != MID_X else MID_W) - 32, HEADER_H - 16,
             size=15, color="#FFFFFF", bold=True, align="center")


def add_step(cid, x, y, title, lines, fill, stroke, circle, title_size=13.5, body_size=11):
    add_vertex(
        cid, "",
        f"rounded=1;arcSize=4;whiteSpace=wrap;html=1;fillColor={fill};strokeColor={stroke};strokeWidth=1.4;",
        x, y, LEFT_W, ROW_H,
    )
    add_vertex(
        f"{cid}_num", "",
        f"ellipse;whiteSpace=wrap;html=1;fillColor={circle};strokeColor=none;fontColor=#FFFFFF;fontSize=12;fontStyle=1;",
        x + 14, y + 14, CIRCLE, CIRCLE,
    )
    add_text(f"{cid}_num_t", str(int(cid[1:])), x + 14, y + 14, CIRCLE, CIRCLE,
             size=12, color="#FFFFFF", bold=True, align="center")
    add_text(f"{cid}_title", title, x + 52, y + 8, LEFT_W - 68, 22,
             size=title_size, color=COLORS["text"], bold=True)
    body = "<br>".join(f"<span style='color:{stroke}'>•</span> {esc(ln)}" for ln in lines)
    add_vertex(
        f"{cid}_body",
        f"<div style='font-size:{body_size}px;color:#334155;line-height:1.45'>{body}</div>",
        "text;html=1;strokeColor=none;fillColor=none;align=left;verticalAlign=top;whiteSpace=wrap;spacing=0;",
        x + 52, y + 36, LEFT_W - 66, ROW_H - 42,
    )


def add_center(cid, y, title, line):
    add_vertex(
        cid, "",
        f"rounded=1;arcSize=5;whiteSpace=wrap;html=1;fillColor={COLORS['mid_fill']};strokeColor={COLORS['mid_stroke']};strokeWidth=1.3;",
        MID_X, y + 10, MID_W, ROW_H - 20,
    )
    add_text(f"{cid}_t", title, MID_X + 12, y + 22, MID_W - 24, 22,
             size=11.8, color=COLORS["mid_header"], bold=True, align="center")
    add_text(f"{cid}_b", line, MID_X + 12, y + 48, MID_W - 24, 40,
             size=10.1, color=COLORS["body"], align="center")


def add_arrow(cid, src, dst, color, dashed=False, exit_x=0.5, entry_x=0.5):
    style = (
        "edgeStyle=none;rounded=0;html=1;"
        f"exitX={exit_x};exitY=1;exitDx=0;exitDy=0;entryX={entry_x};entryY=0;entryDx=0;entryDy=0;"
        "endArrow=block;endSize=7;endFill=1;"
        f"strokeWidth=1.6;strokeColor={color};"
    )
    if dashed:
        style += "dashed=1;dashPattern=4 3;"
    cells.append(
        f'<mxCell id="{cid}" style="{style}" edge="1" parent="1" source="{src}" target="{dst}">'
        '<mxGeometry relative="1" as="geometry"/></mxCell>'
    )


def add_cross_arrow(cid, src, dst, color):
    style = (
        "edgeStyle=none;rounded=0;html=1;"
        "exitX=1;exitY=0.5;exitDx=0;exitDy=0;entryX=0;entryY=0.5;entryDx=0;entryDy=0;"
        "endArrow=block;endSize=6;endFill=1;strokeWidth=1.2;"
        f"strokeColor={color};dashed=1;dashPattern=4 3;"
    )
    cells.append(
        f'<mxCell id="{cid}" style="{style}" edge="1" parent="1" source="{src}" target="{dst}">'
        '<mxGeometry relative="1" as="geometry"/></mxCell>'
    )


def add_legend_arrow(cid, x, y, color, dashed=False):
    style = (
        "edgeStyle=none;rounded=0;html=1;endArrow=block;endSize=6;endFill=1;"
        f"strokeWidth=1.6;strokeColor={color};"
    )
    if dashed:
        style += "dashed=1;dashPattern=4 3;"
    cells.append(
        f'<mxCell id="{cid}" style="{style}" edge="1" parent="1">'
        f'<mxGeometry relative="1" as="geometry"><mxPoint x="{x}" y="{y}" as="sourcePoint"/>'
        f'<mxPoint x="{x + 42}" y="{y}" as="targetPoint"/></mxGeometry></mxCell>'
    )


# title
add_text("title", "会话链路优化前后对比", 40, 24, 1400, 38, size=28, bold=True)
add_text("subtitle", "2026-10-30 · 候选优先检索 / 会话记忆回灌 / 类型化事件 / 上下文预算 / 安静 UI",
         42, 66, 1500, 24, size=12, color="#475569")

# legend
add_legend_arrow("legend_before", 1660, 48, COLORS["before_arrow"])
add_text("legend_before_t", "优化前链路", 1714, 36, 180, 24, size=10, color="#475569")
add_legend_arrow("legend_after", 1660, 76, COLORS["after_arrow"])
add_text("legend_after_t", "优化后链路", 1714, 64, 180, 24, size=10, color="#1D4ED8")
add_legend_arrow("legend_change", 1660, 104, COLORS["mid_arrow"], dashed=True)
add_text("legend_change_t", "对应变化", 1714, 92, 180, 24, size=10, color="#B45309")

# lane headers
add_lane_header("hdr_before", "优化前 · 旧会话链路", LEFT_X, COLORS["before_header"])
add_lane_header("hdr_mid", "关键变化", MID_X, COLORS["mid_header"])
add_lane_header("hdr_after", "优化后 · 当前会话链路", RIGHT_X, COLORS["after_header"])

# rows
for i, row in enumerate(ROWS):
    y = ROW_Y0 + i * ROW_PITCH
    before_id = f"b{i + 1}"
    after_id = f"a{i + 1}"
    mid_id = f"m{i + 1}"
    add_step(before_id, LEFT_X, y, row["title"], row["before"],
             COLORS["before_fill"], COLORS["before_stroke"], COLORS["before_circle"])
    add_step(after_id, RIGHT_X, y, row["title"], row["after"],
             COLORS["after_fill"], COLORS["after_stroke"], COLORS["after_circle"])
    add_center(mid_id, y, row["change"][0], row["change"][1])
    add_cross_arrow(f"x{i + 1}_l", before_id, mid_id, COLORS["mid_arrow"])
    add_cross_arrow(f"x{i + 1}_r", mid_id, after_id, COLORS["mid_arrow"])
    if i < len(ROWS) - 1:
        add_arrow(f"fb{i + 1}", before_id, f"b{i + 2}", COLORS["before_arrow"])
        add_arrow(f"fa{i + 1}", after_id, f"a{i + 2}", COLORS["after_arrow"])

add_text("footer",
         "依据：wiki/04-conversation-turns.md · wiki/05-agent-runtime.md · wiki/06-tools-and-evidence.md · src/agents/ 与 frontend/src/pages/chat/ 当前改动",
         60, 1670, 1900, 20, size=9, color="#64748B")

# --------------------------------------------------------------------------------
xml = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<mxfile host="app.diagrams.net" modified="2026-10-30T12:00:00.000Z" agent="OpenCode" version="24.7.17" type="device">\n'
    '  <diagram id="hdb-conversation-optimization-before-after" name="会话优化前后对比">\n'
    f'    <mxGraphModel dx="0" dy="0" grid="0" gridSize="10" guides="1" tooltips="1" connect="1" arrows="1" fold="1" page="1" pageScale="1" pageWidth="{W}" pageHeight="{H}" background="#FFFFFF" math="0" shadow="0">\n'
    "      <root>\n"
    '        <mxCell id="0"/>\n'
    '        <mxCell id="1" parent="0"/>\n'
    + "".join(f"        {cell}\n" for cell in cells)
    + "      </root>\n"
    "    </mxGraphModel>\n"
    "  </diagram>\n"
    "</mxfile>\n"
)
with open(DRAWIO_PATH, "w", encoding="utf-8") as f:
    f.write(xml)
print(f"wrote {DRAWIO_PATH} ({len(xml):,} bytes, {len(cells)} cells)")

# --------------------------------------------------------------------------------
# PIL preview
CJK_PATH = "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf"
LATIN_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
BOLD_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def is_cjk(ch: str) -> bool:
    return ord(ch) >= 0x2E80 or ch in "（）：；，。！？、《》【】“”‘’…—"


def split_runs(text: str):
    runs, cur, cur_cjk = [], "", None
    for ch in text:
        c = is_cjk(ch)
        if cur_cjk is None or c == cur_cjk:
            cur += ch
        else:
            runs.append((cur, cur_cjk))
            cur = ch
        cur_cjk = c
    if cur:
        runs.append((cur, cur_cjk))
    return runs


def font_for(ch: str, size: int, bold: bool = False):
    if is_cjk(ch):
        return ImageFont.truetype(CJK_PATH, size=size)
    return ImageFont.truetype(BOLD_PATH if bold else LATIN_PATH, size=size)


def mlen(draw, text: str, size: int) -> float:
    return sum(draw.textlength(run, font=font_for(run[0], size)) for run, _ in split_runs(text))


def draw_mixed(draw, xy, text: str, size: int, fill: str, bold: bool = False) -> None:
    x, y = xy
    for run, _ in split_runs(text):
        f = font_for(run[0], size, bold)
        draw.text((x, y), run, font=f, fill=fill)
        x += draw.textlength(run, font=f)


def draw_center(draw, cx, y, text, size, fill, bold=False):
    draw_mixed(draw, (cx - mlen(draw, text, size) / 2, y), text, size, fill, bold)


def wrap_mixed(draw, text: str, size: int, max_w: float) -> list[str]:
    lines, cur = [], ""
    for ch in text:
        cand = cur + ch
        if mlen(draw, cand, size) <= max_w or not cur:
            cur = cand
        else:
            idx = cur.rfind(" ")
            if idx > 0:
                lines.append(cur[:idx])
                cur = cur[idx + 1:] + ch
            else:
                lines.append(cur)
                cur = ch
    if cur:
        lines.append(cur)
    return lines


def dashed(draw, x1, y1, x2, y2, fill, width=2, dash=6, gap=4):
    length = ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5
    if length == 0:
        return
    ux, uy = (x2 - x1) / length, (y2 - y1) / length
    pos = 0
    while pos < length:
        end = min(pos + dash, length)
        draw.line([x1 + ux * pos, y1 + uy * pos, x1 + ux * end, y1 + uy * end], fill=fill, width=width)
        pos += dash + gap


def arrow(draw, x1, y1, x2, y2, fill, width=2, dashed_line=False):
    if dashed_line:
        dashed(draw, x1, y1, x2, y2, fill, width=width)
    else:
        draw.line([x1, y1, x2, y2], fill=fill, width=width)
    if x1 == x2:
        draw.polygon([(x2 - 4, y2 - 8), (x2, y2), (x2 + 4, y2 - 8)], fill=fill)
    else:
        draw.polygon([(x2 - 8, y2 - 4), (x2, y2), (x2 - 8, y2 + 4)], fill=fill)


img = Image.new("RGB", (W, H), "#FFFFFF")
draw = ImageDraw.Draw(img)

# title + subtitle
draw_mixed(draw, (40, 24), "会话链路优化前后对比", 27, "#0F172A", bold=True)
draw_mixed(draw, (42, 66), "2026-10-30 · 候选优先检索 / 会话记忆回灌 / 类型化事件 / 上下文预算 / 安静 UI", 11, "#475569")

# legend
draw.line([1660, 48, 1698, 48], fill=COLORS["before_arrow"], width=2)
draw.polygon([(1698, 44), (1706, 48), (1698, 52)], fill=COLORS["before_arrow"])
draw_mixed(draw, (1714, 36), "优化前链路", 10, "#475569")
draw.line([1660, 76, 1698, 76], fill=COLORS["after_arrow"], width=2)
draw.polygon([(1698, 72), (1706, 76), (1698, 80)], fill=COLORS["after_arrow"])
draw_mixed(draw, (1714, 64), "优化后链路", 10, "#1D4ED8")
dashed(draw, 1660, 104, 1698, 104, COLORS["mid_arrow"], width=2)
draw.polygon([(1698, 100), (1706, 104), (1698, 108)], fill=COLORS["mid_arrow"])
draw_mixed(draw, (1714, 92), "对应变化", 10, "#B45309")

# headers
for x, w, title, fill in [
    (LEFT_X, LEFT_W, "优化前 · 旧会话链路", COLORS["before_header"]),
    (MID_X, MID_W, "关键变化", COLORS["mid_header"]),
    (RIGHT_X, RIGHT_W, "优化后 · 当前会话链路", COLORS["after_header"]),
]:
    draw.rounded_rectangle([x, HEADER_Y, x + w, HEADER_Y + HEADER_H], radius=6, fill=fill)
    draw_center(draw, x + w / 2, HEADER_Y + 16, title, 14, "#FFFFFF", bold=True)

for i, row in enumerate(ROWS):
    y = ROW_Y0 + i * ROW_PITCH
    for side, x, fill, stroke, circle in [
        ("b", LEFT_X, COLORS["before_fill"], COLORS["before_stroke"], COLORS["before_circle"]),
        ("a", RIGHT_X, COLORS["after_fill"], COLORS["after_stroke"], COLORS["after_circle"]),
    ]:
        lines = row["before"] if side == "b" else row["after"]
        draw.rounded_rectangle([x, y, x + LEFT_W, y + ROW_H], radius=5, fill=fill, outline=stroke, width=2)
        draw.ellipse([x + 14, y + 14, x + 14 + CIRCLE, y + 14 + CIRCLE], fill=circle)
        draw_center(draw, x + 14 + CIRCLE / 2, y + 19, str(row["n"]), 12, "#FFFFFF", bold=True)
        draw_mixed(draw, (x + 52, y + 9), row["title"], 13, "#0F172A", bold=True)
        by = y + 37
        for ln in lines:
            draw.ellipse([x + 53, by + 4, x + 56, by + 7], fill=stroke)
            for j, sub in enumerate(wrap_mixed(draw, ln, 10.8, LEFT_W - 70)):
                draw_mixed(draw, (x + 61, by), sub, 10.8, "#334155")
                by += 16
            by += 1
    # center card
    draw.rounded_rectangle([MID_X, y + 10, MID_X + MID_W, y + ROW_H - 10], radius=5,
                           fill=COLORS["mid_fill"], outline=COLORS["mid_stroke"], width=2)
    draw_center(draw, MID_X + MID_W / 2, y + 22, row["change"][0], 11.5, COLORS["mid_header"], bold=True)
    draw_center(draw, MID_X + MID_W / 2, y + 48, row["change"][1], 10, "#334155")
    # cross arrows
    arrow(draw, LEFT_X + LEFT_W + 2, y + ROW_H / 2, MID_X - 2, y + ROW_H / 2, COLORS["mid_arrow"], width=1, dashed_line=True)
    arrow(draw, MID_X + MID_W + 2, y + ROW_H / 2, RIGHT_X - 2, y + ROW_H / 2, COLORS["mid_arrow"], width=1, dashed_line=True)
    # vertical lane arrows
    if i < len(ROWS) - 1:
        arrow(draw, LEFT_X + LEFT_W / 2, y + ROW_H + 1, LEFT_X + LEFT_W / 2, y + ROW_PITCH - 2,
              COLORS["before_arrow"], width=2)
        arrow(draw, RIGHT_X + RIGHT_W / 2, y + ROW_H + 1, RIGHT_X + RIGHT_W / 2, y + ROW_PITCH - 2,
              COLORS["after_arrow"], width=2)

draw_mixed(draw, (60, 1668),
           "依据：wiki/04-conversation-turns.md · wiki/05-agent-runtime.md · wiki/06-tools-and-evidence.md · src/agents/ 与 frontend/src/pages/chat/ 当前改动",
           8.5, "#64748B")

img.save(PREVIEW_PATH)
print(f"wrote {PREVIEW_PATH} ({W}x{H})")
