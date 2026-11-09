#!/usr/bin/env python3
"""Generate the workspace-first general-agent upgrade diagram (2026-11)."""
from __future__ import annotations

import html
import os

from PIL import Image, ImageDraw, ImageFont

OUT_DIR = os.path.dirname(os.path.abspath(__file__))
DRAWIO_PATH = os.path.join(OUT_DIR, "general-agent-upgrade.drawio")
PREVIEW_PATH = os.path.join(OUT_DIR, "general-agent-upgrade.preview.png")

W, H = 2040, 1580
LEFT_X, LEFT_W = 60, 780
MID_X, MID_W = 880, 300
RIGHT_X, RIGHT_W = 1220, 780
HEADER_Y, HEADER_H = 124, 52
ROW_Y0, ROW_H, ROW_PITCH = 196, 132, 150
CIRCLE = 26

LAYER_Y, LAYER_H, LAYER_GAP = 1164, 118, 20
LAYER_W = (1940 - 4 * LAYER_GAP) // 5
GOV_Y, GOV_H = 1298, 40
BOUND_Y, BOUND_H, BOUND_GAP = 1400, 86, 16
BOUND_W = (1940 - 5 * BOUND_GAP) // 6

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
    "layer": "#0F766E",
    "layer_fill": "#F0FDFA",
    "layer_stroke": "#14B8A6",
    "bound": "#9A3412",
    "bound_fill": "#FFF7ED",
    "bound_stroke": "#FDBA74",
    "text": "#0F172A",
    "body": "#334155",
    "muted": "#64748B",
}

ROWS = [
    {
        "n": 1,
        "title": "项目容器",
        "before": [
            "会话只承载单次问答",
            "知识库是会话的前置上下文",
            "没有跨会话的项目容器",
        ],
        "after": [
            "Workspace = 项目容器，kind=general|knowledge",
            "1 个 workspace 下挂 1..N 个 session",
            "知识库只是可选绑定，未绑定即通用工作区",
        ],
        "change": ("Workspace-first", "从一次问答到持续工作区"),
    },
    {
        "n": 2,
        "title": "执行模型",
        "before": [
            "聊天 turn 自动创建 task / task_pending",
            "聊天与任务工作台共用 task 模型",
            "会话结束即任务结束",
        ],
        "after": [
            "聊天 turn 用 trace_context.workspace_id 关联",
            "不建 task，复用 turn 队列 / fencing / SSE",
            "显式 /tasks 保留，task 模型不变",
        ],
        "change": ("聊天与任务解耦", "少一层任务生命周期"),
    },
    {
        "n": 3,
        "title": "工具与能力",
        "before": [
            "固定知识库问答流程",
            "文件能力弱，主要靠检索",
            "工具由模式 / 意图决定",
        ],
        "after": [
            "workspace_list/read/write/edit/glob/grep",
            "workspace_publish_artifact 发布交付物",
            "kb_search、表格 SQL、网表、Skill、MCP 都是工具",
        ],
        "change": ("通用工具集", "不预设流程，模型自主规划"),
    },
    {
        "n": 4,
        "title": "产物与交付",
        "before": [
            "产物绑定 task，在工作台查看",
            "同名文件覆盖，没有版本",
            "前端独立产物区域",
        ],
        "after": [
            "内部写 workspace/<id>/out/，对外只给文件名",
            "artifacts 版本化，同名自动递增",
            "按 session_id + turn_id 挂到 assistant 消息",
        ],
        "change": ("产物版本化", "交付物跟回答走，可下载"),
    },
    {
        "n": 5,
        "title": "隔离与存储",
        "before": [
            "独立 tenant 层 + tenant_id 贯穿",
            "用户 / 部门 / 租户三层边界",
            "存储后端需人工保证一致",
        ],
        "after": [
            "user_id 即数据归属和权限边界",
            "部门只是共享 / 可见性分组",
            "WorkspaceStore 跟随控制面 PG，启动迁移清旧表",
        ],
        "change": ("用户即租户", "边界更简单，多实例不分裂"),
    },
    {
        "n": 6,
        "title": "前端呈现",
        "before": [
            "产物独立区域展示，和回答分离",
            "运行时编排事件进入聊天轨迹",
            "流式身份文案混用检索 / 生成身份",
        ],
        "after": [
            "产物按 turn_id 挂在 assistant 消息下",
            "「处理过程」只投影真实工具动作",
            "流式统一「正在处理」，过程与结论分离",
        ],
        "change": ("产物跟随回答", "聊天轨迹更干净"),
    },
]

LAYERS = [
    ("接入层", ["Web 对话 · REST API", "MCP 桥 · 外部 Agent"]),
    ("工作区层", ["Workspace（general | knowledge）", "Session 1..N"]),
    ("执行层", ["Turn / Run · 队列 / fencing / SSE", "Agent Loop · 上下文压缩"]),
    ("工具层", ["workspace_* · kb_search · SQL/表格", "网表 · Skill · MCP"]),
    ("数据层", ["workspace/out · Artifacts", "KB 解析库 · PG/SQLite"]),
]

BOUNDARIES = [
    ("执行能力", ["通用 shell / 代码执行未接入；", "Sandbox 仍为 bundle 制"]),
    ("Workspace UI", ["前端仍是知识库会话列表；", "文件视图下一阶段"]),
    ("对象存储", ["解析文件 / 产物仍可能落本地；", "SaaS 需 S3/MinIO"]),
    ("KB 命名空间", ["RAGFlow 物理数据集仍需", "按部门命名空间隔离"]),
    ("配额", ["per-user 并发 / 存储 / token", "配额尚未配置化"]),
    ("HITL", ["危险动作审批流待", "执行能力接入后补"]),
]


def esc(v: str) -> str:
    return html.escape(v, quote=True)


cells: list[str] = []


def add_vertex(cid, value, style, x, y, w, h):
    cells.append(
        f'<mxCell id="{cid}" value="{esc(value)}" style="{style}" vertex="1" parent="1">'
        f'<mxGeometry x="{x}" y="{y}" width="{w}" height="{h}" as="geometry"/></mxCell>'
    )


def add_text(cid, value, x, y, w, h, size=12, color="#0F172A", bold=False,
             align="left", valign="middle"):
    style = (
        f"text;html=1;strokeColor=none;fillColor=none;align={align};verticalAlign={valign};"
        f"whiteSpace=wrap;fontSize={size};fontColor={color};"
    )
    if bold:
        style += "fontStyle=1;"
    add_vertex(cid, value, style, x, y, w, h)


def add_lane_header(cid, title, x, width, fill):
    add_vertex(
        cid, "",
        f"rounded=1;arcSize=6;whiteSpace=wrap;html=1;fillColor={fill};strokeColor={fill};strokeWidth=1;",
        x, HEADER_Y, width, HEADER_H,
    )
    add_text(f"{cid}_t", title, x + 16, HEADER_Y + 8, width - 32, HEADER_H - 16,
             size=14.5, color="#FFFFFF", bold=True, align="center")


def add_step(cid, x, y, number, title, lines, fill, stroke, circle):
    add_vertex(
        cid, "",
        f"rounded=1;arcSize=4;whiteSpace=wrap;html=1;fillColor={fill};strokeColor={stroke};strokeWidth=1.4;",
        x, y, LEFT_W, ROW_H,
    )
    add_vertex(
        f"{cid}_num", "",
        f"ellipse;whiteSpace=wrap;html=1;fillColor={circle};strokeColor=none;",
        x + 14, y + 14, CIRCLE, CIRCLE,
    )
    add_text(f"{cid}_num_t", str(number), x + 14, y + 14, CIRCLE, CIRCLE,
             size=11.5, color="#FFFFFF", bold=True, align="center")
    add_text(f"{cid}_title", title, x + 50, y + 8, LEFT_W - 64, 22,
             size=13, color=COLORS["text"], bold=True)
    body = "<br>".join(f"<span style='color:{stroke}'>•</span> {esc(ln)}" for ln in lines)
    add_vertex(
        f"{cid}_body",
        f"<div style='font-size:10.8px;color:#334155;line-height:1.45'>{body}</div>",
        "text;html=1;strokeColor=none;fillColor=none;align=left;verticalAlign=top;"
        "whiteSpace=wrap;spacing=0;",
        x + 50, y + 36, LEFT_W - 62, ROW_H - 42,
    )


def add_center(cid, y, title, line):
    add_vertex(
        cid, "",
        f"rounded=1;arcSize=5;whiteSpace=wrap;html=1;fillColor={COLORS['mid_fill']};"
        f"strokeColor={COLORS['mid_stroke']};strokeWidth=1.3;",
        MID_X, y + 10, MID_W, ROW_H - 20,
    )
    add_text(f"{cid}_t", title, MID_X + 12, y + 24, MID_W - 24, 22,
             size=11.8, color=COLORS["mid_header"], bold=True, align="center")
    add_text(f"{cid}_b", line, MID_X + 12, y + 52, MID_W - 24, 40,
             size=10.1, color=COLORS["body"], align="center")


def add_cross_arrow(cid, src, dst):
    style = (
        "edgeStyle=none;rounded=0;html=1;"
        "exitX=1;exitY=0.5;exitDx=0;exitDy=0;entryX=0;entryY=0.5;entryDx=0;entryDy=0;"
        "endArrow=block;endSize=6;endFill=1;strokeWidth=1.2;"
        f"strokeColor={COLORS['mid_arrow']};dashed=1;dashPattern=4 3;"
    )
    cells.append(
        f'<mxCell id="{cid}" style="{style}" edge="1" parent="1" source="{src}" target="{dst}">'
        '<mxGeometry relative="1" as="geometry"/></mxCell>'
    )


def add_vertical_arrow(cid, src, dst, color):
    style = (
        "edgeStyle=none;rounded=0;html=1;"
        "exitX=0.5;exitY=1;exitDx=0;exitDy=0;entryX=0.5;entryY=0;entryDx=0;entryDy=0;"
        "endArrow=block;endSize=7;endFill=1;"
        f"strokeWidth=1.6;strokeColor={color};"
    )
    cells.append(
        f'<mxCell id="{cid}" style="{style}" edge="1" parent="1" source="{src}" target="{dst}">'
        '<mxGeometry relative="1" as="geometry"/></mxCell>'
    )


def add_layer_box(cid, x, title, lines):
    content = (
        f"<div style='font-size:13px;font-weight:bold;color:{COLORS['layer']};"
        "margin-bottom:5px'>" + esc(title) + "</div>"
        f"<div style='font-size:10.5px;color:#334155;line-height:1.45'>"
        + "<br>".join(esc(line) for line in lines) + "</div>"
    )
    add_vertex(
        cid, content,
        f"rounded=1;arcSize=7;whiteSpace=wrap;html=1;fillColor={COLORS['layer_fill']};"
        f"strokeColor={COLORS['layer_stroke']};strokeWidth=1.2;align=left;"
        "verticalAlign=middle;spacing=12;",
        x, LAYER_Y, LAYER_W, LAYER_H,
    )


def add_boundary_chip(cid, x, title, lines):
    content = (
        f"<div style='font-size:11.5px;font-weight:bold;color:{COLORS['bound']};"
        "margin-bottom:4px'>" + esc(title) + "</div>"
        f"<div style='font-size:9.6px;color:#7C2D12;line-height:1.35'>"
        + "<br>".join(esc(line) for line in lines) + "</div>"
    )
    add_vertex(
        cid, content,
        f"rounded=1;arcSize=8;whiteSpace=wrap;html=1;fillColor={COLORS['bound_fill']};"
        f"strokeColor={COLORS['bound_stroke']};strokeWidth=1.1;align=left;"
        "verticalAlign=middle;spacing=10;",
        x, BOUND_Y, BOUND_W, BOUND_H,
    )


# ---------------------------------------------------------------------------
# draw.io XML
# ---------------------------------------------------------------------------

add_text("title", "通用 Agent 架构升级", 40, 22, 1100, 40, size=28, bold=True)
add_text(
    "subtitle",
    "Workspace-first：工作区承载会话与产物 · 聊天不再自动建任务 · 用户即租户",
    42, 68, 1500, 24, size=12, color="#475569",
)
add_text("stamp", "2026-11-06 · 汇报版", 1700, 28, 300, 22,
         size=10.5, color=COLORS["muted"], align="right")

add_lane_header("hdr_before", "升级前 · Task-centric", LEFT_X, LEFT_W, COLORS["before_header"])
add_lane_header("hdr_mid", "关键变化", MID_X, MID_W, COLORS["mid_header"])
add_lane_header("hdr_after", "升级后 · Workspace-first", RIGHT_X, RIGHT_W, COLORS["after_header"])

for i, row in enumerate(ROWS):
    y = ROW_Y0 + i * ROW_PITCH
    before_id = f"b{i + 1}"
    after_id = f"a{i + 1}"
    mid_id = f"m{i + 1}"
    add_step(before_id, LEFT_X, y, row["n"], row["title"], row["before"],
             COLORS["before_fill"], COLORS["before_stroke"], COLORS["before_circle"])
    add_step(after_id, RIGHT_X, y, row["n"], row["title"], row["after"],
             COLORS["after_fill"], COLORS["after_stroke"], COLORS["after_circle"])
    add_center(mid_id, y, row["change"][0], row["change"][1])
    add_cross_arrow(f"x{i + 1}_l", before_id, mid_id)
    add_cross_arrow(f"x{i + 1}_r", mid_id, after_id)
    if i < len(ROWS) - 1:
        add_vertical_arrow(f"fb{i + 1}", before_id, f"b{i + 2}", COLORS["before_arrow"])
        add_vertical_arrow(f"fa{i + 1}", after_id, f"a{i + 2}", COLORS["after_arrow"])

add_text("layer_title", "升级后的通用 Agent 分层", 60, 1108, 900, 30,
         size=17, bold=True)
add_text("layer_note", "Workspace → Session → Turn/Run → Agent Loop → Tools → Data",
         900, 1114, 1040, 24, size=10.5, color=COLORS["muted"], align="right")

for i, (title, lines) in enumerate(LAYERS):
    x = LEFT_X + i * (LAYER_W + LAYER_GAP)
    add_layer_box(f"layer{i + 1}", x, title, lines)
    if i < len(LAYERS) - 1:
        style = (
            "edgeStyle=none;rounded=0;html=1;"
            "exitX=1;exitY=0.5;exitDx=0;exitDy=0;entryX=0;entryY=0.5;entryDx=0;entryDy=0;"
            "endArrow=block;endSize=7;endFill=1;strokeWidth=1.6;"
            f"strokeColor={COLORS['layer_stroke']};"
        )
        cells.append(
            f'<mxCell id="la{i + 1}" style="{style}" edge="1" parent="1" '
            f'source="layer{i + 1}" target="layer{i + 2}">'
            '<mxGeometry relative="1" as="geometry"/></mxCell>'
        )

add_vertex(
    "governance", "",
    "rounded=0;whiteSpace=wrap;html=1;fillColor=#F1F5F9;strokeColor=#CBD5E1;strokeWidth=1;",
    60, GOV_Y, 1940, GOV_H,
)
add_text(
    "governance_t",
    "横切治理：user_id 隔离 · workspace 路径校验 · 审计 · 证据/引用 · 权限",
    80, GOV_Y + 10, 1900, GOV_H - 20, size=11, color="#334155", align="center",
)

add_text("bound_title", "当前边界（下一阶段）", 60, 1358, 600, 28,
         size=15, bold=True, color=COLORS["bound"])
add_text("bound_note", "不是本次升级的完成项，汇报时建议明确标注",
         660, 1362, 1340, 22, size=10, color=COLORS["muted"], align="right")

for i, (title, lines) in enumerate(BOUNDARIES):
    x = LEFT_X + i * (BOUND_W + BOUND_GAP)
    add_boundary_chip(f"bound{i + 1}", x, title, lines)

add_text(
    "footer",
    "依据：wiki/20-general-agent.md · 5ccb4e8 workspace-first · 2a6dc27 用户即租户 · "
    "src/core/workspace_store.py / src/agents/runner.py 当前实现",
    60, 1530, 1900, 20, size=9, color=COLORS["muted"],
)

xml = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<mxfile host="app.diagrams.net" modified="2026-11-06T12:00:00.000Z" '
    'agent="OpenCode" version="24.7.17" type="device">\n'
    '  <diagram id="hdb-general-agent-upgrade" name="通用 Agent 架构升级">\n'
    f'    <mxGraphModel dx="0" dy="0" grid="0" gridSize="10" guides="1" tooltips="1" '
    f'connect="1" arrows="1" fold="1" page="1" pageScale="1" pageWidth="{W}" '
    f'pageHeight="{H}" background="#FFFFFF" math="0" shadow="0">\n'
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

# ---------------------------------------------------------------------------
# PIL preview (approximate; the .drawio is authoritative)
# ---------------------------------------------------------------------------

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
        draw.line([x1 + ux * pos, y1 + uy * pos, x1 + ux * end, y1 + uy * end],
                  fill=fill, width=width)
        pos += dash + gap


def arrow(draw, x1, y1, x2, y2, fill, width=2, dashed_line=False):
    if dashed_line:
        dashed(draw, x1, y1, x2, y2, fill, width=width)
    else:
        draw.line([x1, y1, x2, y2], fill=fill, width=width)
    if abs(x2 - x1) < abs(y2 - y1):
        draw.polygon([(x2 - 4, y2 - 8), (x2, y2), (x2 + 4, y2 - 8)], fill=fill)
    else:
        draw.polygon([(x2 - 8, y2 - 4), (x2, y2), (x2 - 8, y2 + 4)], fill=fill)


img = Image.new("RGB", (W, H), "#FFFFFF")
draw = ImageDraw.Draw(img)

draw_mixed(draw, (40, 22), "通用 Agent 架构升级", 27, "#0F172A", bold=True)
draw_mixed(draw, (42, 68),
           "Workspace-first：工作区承载会话与产物 · 聊天不再自动建任务 · 用户即租户",
           11, "#475569")
draw_mixed(draw, (1700, 30), "2026-11-06 · 汇报版", 10, COLORS["muted"])

for x, w, title, fill in [
    (LEFT_X, LEFT_W, "升级前 · Task-centric", COLORS["before_header"]),
    (MID_X, MID_W, "关键变化", COLORS["mid_header"]),
    (RIGHT_X, RIGHT_W, "升级后 · Workspace-first", COLORS["after_header"]),
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
        draw.rounded_rectangle([x, y, x + LEFT_W, y + ROW_H], radius=5,
                               fill=fill, outline=stroke, width=2)
        draw.ellipse([x + 14, y + 14, x + 14 + CIRCLE, y + 14 + CIRCLE], fill=circle)
        draw_center(draw, x + 14 + CIRCLE / 2, y + 19, str(row["n"]), 11, "#FFFFFF", bold=True)
        draw_mixed(draw, (x + 50, y + 9), row["title"], 12.5, "#0F172A", bold=True)
        by = y + 37
        for ln in lines:
            draw.ellipse([x + 51, by + 4, x + 54, by + 7], fill=stroke)
            for sub in wrap_mixed(draw, ln, 10.4, LEFT_W - 68):
                draw_mixed(draw, (x + 59, by), sub, 10.4, "#334155")
                by += 15.5
            by += 1

    draw.rounded_rectangle([MID_X, y + 10, MID_X + MID_W, y + ROW_H - 10], radius=5,
                           fill=COLORS["mid_fill"], outline=COLORS["mid_stroke"], width=2)
    draw_center(draw, MID_X + MID_W / 2, y + 24, row["change"][0], 11.5,
                COLORS["mid_header"], bold=True)
    draw_center(draw, MID_X + MID_W / 2, y + 52, row["change"][1], 10, "#334155")
    arrow(draw, LEFT_X + LEFT_W + 2, y + ROW_H / 2, MID_X - 2, y + ROW_H / 2,
          COLORS["mid_arrow"], width=1, dashed_line=True)
    arrow(draw, MID_X + MID_W + 2, y + ROW_H / 2, RIGHT_X - 2, y + ROW_H / 2,
          COLORS["mid_arrow"], width=1, dashed_line=True)
    if i < len(ROWS) - 1:
        arrow(draw, LEFT_X + LEFT_W / 2, y + ROW_H + 1, LEFT_X + LEFT_W / 2,
              y + ROW_PITCH - 2, COLORS["before_arrow"], width=2)
        arrow(draw, RIGHT_X + RIGHT_W / 2, y + ROW_H + 1, RIGHT_X + RIGHT_W / 2,
              y + ROW_PITCH - 2, COLORS["after_arrow"], width=2)

draw_mixed(draw, (60, 1108), "升级后的通用 Agent 分层", 16, "#0F172A", bold=True)
draw_mixed(draw, (900, 1114),
           "Workspace → Session → Turn/Run → Agent Loop → Tools → Data",
           10, COLORS["muted"])

for i, (title, lines) in enumerate(LAYERS):
    x = LEFT_X + i * (LAYER_W + LAYER_GAP)
    draw.rounded_rectangle([x, LAYER_Y, x + LAYER_W, LAYER_Y + LAYER_H], radius=7,
                           fill=COLORS["layer_fill"], outline=COLORS["layer_stroke"], width=2)
    draw_mixed(draw, (x + 12, LAYER_Y + 14), title, 12.5, COLORS["layer"], bold=True)
    by = LAYER_Y + 40
    for ln in lines:
        for sub in wrap_mixed(draw, ln, 10.2, LAYER_W - 24):
            draw_mixed(draw, (x + 12, by), sub, 10.2, "#334155")
            by += 15
    if i < len(LAYERS) - 1:
        arrow(draw, x + LAYER_W + 2, LAYER_Y + LAYER_H / 2,
              x + LAYER_W + LAYER_GAP - 2, LAYER_Y + LAYER_H / 2,
              COLORS["layer_stroke"], width=2)

draw.rounded_rectangle([60, GOV_Y, 2000, GOV_Y + GOV_H], radius=0,
                       fill="#F1F5F9", outline="#CBD5E1", width=1)
draw_center(draw, 1030, GOV_Y + 11,
            "横切治理：user_id 隔离 · workspace 路径校验 · 审计 · 证据/引用 · 权限",
            10.5, "#334155")

draw_mixed(draw, (60, 1358), "当前边界（下一阶段）", 14, COLORS["bound"], bold=True)
draw_mixed(draw, (660, 1362), "不是本次升级的完成项，汇报时建议明确标注",
           9.5, COLORS["muted"])

for i, (title, lines) in enumerate(BOUNDARIES):
    x = LEFT_X + i * (BOUND_W + BOUND_GAP)
    draw.rounded_rectangle([x, BOUND_Y, x + BOUND_W, BOUND_Y + BOUND_H], radius=7,
                           fill=COLORS["bound_fill"], outline=COLORS["bound_stroke"], width=1)
    draw_mixed(draw, (x + 10, BOUND_Y + 10), title, 11, COLORS["bound"], bold=True)
    by = BOUND_Y + 32
    for ln in lines:
        for sub in wrap_mixed(draw, ln, 9.2, BOUND_W - 20):
            draw_mixed(draw, (x + 10, by), sub, 9.2, "#7C2D12")
            by += 13

draw_mixed(
    draw,
    (60, 1530),
    "依据：wiki/20-general-agent.md · 5ccb4e8 workspace-first · 2a6dc27 用户即租户 · "
    "src/core/workspace_store.py / src/agents/runner.py 当前实现",
    8.5, COLORS["muted"],
)

img.save(PREVIEW_PATH)
print(f"wrote {PREVIEW_PATH} ({W}x{H})")
