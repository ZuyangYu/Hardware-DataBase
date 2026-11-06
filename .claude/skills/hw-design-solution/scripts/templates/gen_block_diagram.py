# -*- coding: utf-8 -*-
"""② 系统框图生成模板 v1 —— 数据驱动架构（规则见 references/02-block-diagram.md §2/§3/§9）
注册表（ZONES/BLOCKS/EDGES/LABELS）是唯一数据源：改数据不重写逻辑。
- 三层 z-order：zone → edge → node；root 固定 <mxCell id="0"/><mxCell id="1" parent="0"/>
- 端点用 P(块名, 边, 偏移) 引用，禁止手写散落绝对坐标
- 黑信号线全部 jumpStyle=arc；电源线红线走断线画法（段A/段B，电压名=配对唯一依据）
- 独立标签 fontsize=25；value 一律 html.escape(s, quote=True)
输出：_out/demo_block_diagram.drawio（HW_DESIGN_OUT 可覆盖目录）
"""
import html
import math
import os

OUT_DIR = os.environ.get("HW_DESIGN_OUT") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "_out")
os.makedirs(OUT_DIR, exist_ok=True)
OUT = os.path.join(OUT_DIR, "demo_block_diagram.drawio")


def B(name, x, y, w, h, label="", style=""):
    return {"name": name, "x": x, "y": y, "w": w, "h": h, "label": label, "style": style}


def P(block, side, pos):
    """端点引用：块名 + 边（L/R/T/B）+ 沿边偏移"""
    return {"block": block, "side": side, "pos": pos}


# ── 样式 ──
S_ZONE = "rounded=0;fillColor=#F5F5F5;strokeColor=#666666;verticalAlign=top;fontSize=14;fontStyle=1;whiteSpace=wrap;"
S_BLOCK = "rounded=0;fillColor=#DAE8FC;strokeColor=#6C8EBF;fontSize=13;whiteSpace=wrap;"
S_CONN = "rounded=0;fillColor=#FFE6CC;strokeColor=#D79B00;fontSize=13;whiteSpace=wrap;"
S_EDGE = "edgeStyle=orthogonalEdgeStyle;rounded=0;jumpStyle=arc;jumpSize=10;strokeColor=#000000;endArrow=block;endFill=1;"
S_PWR = "edgeStyle=orthogonalEdgeStyle;rounded=0;strokeColor=#FF0000;strokeWidth=2;endArrow=block;endFill=1;"
S_TEXT = "text;html=1;align=center;verticalAlign=middle;fontSize=25;fontColor=#000000;whiteSpace=wrap;"

# ── 注册表（唯一数据源） ──
ZONES = [
    B("zone_soc", 40, 40, 1150, 1010, "SOC 板", S_ZONE),
    B("zone_dmd", 1330, 40, 1150, 1010, "DMD 板", S_ZONE),
]
BLOCKS = [
    B("buck33", 140, 120, 220, 120, "DCDC buck 3.3V\nSA25903", S_BLOCK),
    B("can",    560, 120, 200, 100, "CANFD 收发器\nTCAN1043", S_BLOCK),
    B("soc",    140, 380, 220, 160, "SOC 主控\nLQ560", S_BLOCK),
    B("dsi",    560, 380, 200, 100, "DSI 转 LVDS\nSN65DSI83", S_BLOCK),
    B("eth",    560, 640, 200, 100, "以太网 PHY\nDP83TC812", S_BLOCK),
    B("conn",   1240, 320, 80, 300, "连接器\nJ2 板对板", S_CONN),
    B("dlpc",   1520, 380, 220, 140, "DLP 控制器\nDLPC231S", S_BLOCK),
    B("dmd",    1520, 640, 220, 110, "DMD 阵列\nDLP5530S", S_BLOCK),
    B("pmic",   1900, 120, 200, 120, "DLP PMIC\nTPS9900S", S_BLOCK),
    B("flash",  1900, 380, 200, 100, "SPI Flash\nGD25F128F", S_BLOCK),
]
# 连线：src/dst 为 None 即断线段（段A=dst None：尾部悬空；段B=src None：头部悬空）；
# pts=中间拐点；断线段的悬空端是 pts 的最后一个（段A）/第一个（段B）点。电压名 pair=配对唯一依据。
EDGES = [
    {"src": P("can", "R", 50),  "dst": P("conn", "L", 40),  "pts": [(1100, 170), (1100, 360)], "style": S_EDGE},
    {"src": P("conn", "R", 140), "dst": P("dlpc", "L", 80), "pts": [], "style": S_EDGE},
    {"src": P("eth", "R", 50),  "dst": P("conn", "L", 260), "pts": [(1140, 690), (1140, 580)], "style": S_EDGE},
    {"src": P("soc", "R", 80),  "dst": P("dsi", "L", 80),   "pts": [], "style": S_EDGE},
    {"src": P("dlpc", "B", 110), "dst": P("dmd", "T", 110), "pts": [], "style": S_EDGE},
    {"src": P("dlpc", "R", 60), "dst": P("flash", "L", 60), "pts": [], "style": S_EDGE},
    {"src": P("dsi", "R", 50),  "dst": P("conn", "L", 140), "pts": [(1165, 430), (1165, 460)], "style": S_EDGE},
    {"src": P("buck33", "R", 60), "dst": None, "pts": [(480, 180)], "style": S_PWR, "pair": "R4 3.3V"},
    {"src": None, "dst": P("pmic", "L", 60), "pts": [(1760, 180)], "style": S_PWR, "pair": "R4 3.3V"},
]
LABELS = [  # (文本, x, y, w, h)——独立 text 标签，fs25，禁骑线
    ("R4 3.3V",   360, 110, 120, 40),
    ("R4 3.3V",   1760, 110, 120, 40),
    ("CAN TX/RX", 800, 100, 150, 40),
    ("LVDS",      980, 360, 90, 40),
    ("LVDS",      1360, 390, 90, 40),
    ("RGMII",     820, 620, 100, 40),
    ("DSI",       400, 390, 80, 40),
    ("DMD 总线",  1660, 550, 130, 40),
    ("SPI",       1780, 370, 70, 40),
]

BY_NAME = {b["name"]: b for b in ZONES + BLOCKS}


def endpoint_pt(ref):
    b = BY_NAME[ref["block"]]
    s, p = ref["side"], ref["pos"]
    if s == "L":
        return (b["x"], b["y"] + p)
    if s == "R":
        return (b["x"] + b["w"], b["y"] + p)
    if s == "T":
        return (b["x"] + p, b["y"])
    if s == "B":
        return (b["x"] + p, b["y"] + b["h"])
    raise ValueError(side_err(s))


def side_err(s):
    return f"未知边 {s}"


def resolve_edge(e):
    """返回 (sourcePoint, targetPoint, waypoints)。段A/段B 的悬空端并入端点坐标。"""
    pts = list(e["pts"])
    sp = endpoint_pt(e["src"]) if e["src"] else None
    tp = endpoint_pt(e["dst"]) if e["dst"] else None
    if sp and tp:
        return sp, tp, pts
    if sp:                      # 段A
        return sp, pts[-1], pts[:-1]
    return pts[0], tp, pts[1:]  # 段B


# ── 自检（改一步验一步）：正交 / 断线长度 / 标签距线 ≥25 ──
def _seg_rect_dist(a, b, r):
    """线段到矩形的最小距离（矩形边缘距离，非中心）"""
    x0, y0, x1, y1 = r["x"], r["y"], r["x"] + r["w"], r["y"] + r["h"]
    edges = [((x0, y0), (x1, y0)), ((x1, y0), (x1, y1)), ((x1, y1), (x0, y1)), ((x0, y1), (x0, y0))]

    def pt_seg(p, s, t):
        dx, dy = t[0] - s[0], t[1] - s[1]
        L2 = dx * dx + dy * dy
        t_ = 0 if L2 == 0 else max(0.0, min(1.0, ((p[0] - s[0]) * dx + (p[1] - s[1]) * dy) / L2))
        return math.hypot(p[0] - s[0] - t_ * dx, p[1] - s[1] - t_ * dy)

    def seg_seg(s1, t1, s2, t2):
        return min(pt_seg(s1, s2, t2), pt_seg(t1, s2, t2), pt_seg(s2, s1, t1), pt_seg(t2, s1, t1))

    return min(seg_seg(a, b, s, t) for s, t in edges)


def selfcheck():
    errs = []
    for e in EDGES:
        sp, tp, wps = resolve_edge(e)
        path = [sp] + wps + [tp]
        for (x0, y0), (x1, y1) in zip(path, path[1:]):
            if x0 != x1 and y0 != y1:
                errs.append(f"斜线 {e['src']}→{e['dst']}")
        if not e["src"] or not e["dst"]:
            L = sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(path, path[1:]))
            if L > 200:
                errs.append(f"断线超长 {L:.0f}px")
    for text, x, y, w, h in LABELS:
        r = {"x": x, "y": y, "w": w, "h": h}
        for e in EDGES:
            sp, tp, wps = resolve_edge(e)
            path = [sp] + wps + [tp]
            d = min(_seg_rect_dist(a, b, r) for a, b in zip(path, path[1:]))
            if d < 25:
                errs.append(f"标签距线 {d:.0f}px <25：{text}")
    return errs


# ── XML 组装（三层顺序：zone → edge → node/text） ──
def esc(s):
    return html.escape(s, quote=True)


def render():
    cells = []

    def vertex(cid, b, style):
        cells.append(
            f'<mxCell id="{cid}" value="{esc(b["label"])}" style="{style}" vertex="1" parent="1">'
            f'<mxGeometry x="{b["x"]}" y="{b["y"]}" width="{b["w"]}" height="{b["h"]}" as="geometry"/></mxCell>')

    def text(cid, text_, x, y, w, h):
        cells.append(
            f'<mxCell id="{cid}" value="{esc(text_)}" style="{S_TEXT}" vertex="1" parent="1">'
            f'<mxGeometry x="{x}" y="{y}" width="{w}" height="{h}" as="geometry"/></mxCell>')

    def edge(cid, e):
        sp, tp, wps = resolve_edge(e)
        arr = ""
        if wps:
            pts = "".join(f'<mxPoint x="{x}" y="{y}"/>' for x, y in wps)
            arr = f'<Array as="points">{pts}</Array>'
        cells.append(
            f'<mxCell id="{cid}" style="{e["style"]}" edge="1" parent="1">'
            f'<mxGeometry relative="1" as="geometry">'
            f'<mxPoint x="{sp[0]}" y="{sp[1]}" as="sourcePoint"/>'
            f'<mxPoint x="{tp[0]}" y="{tp[1]}" as="targetPoint"/>{arr}</mxGeometry></mxCell>')

    for i, z in enumerate(ZONES):
        vertex(f"z{i + 1}", z, z["style"])
    for i, e in enumerate(EDGES):
        edge(f"e{i + 1}", e)
    for i, b in enumerate(BLOCKS):
        vertex(f"b{i + 1}", b, b["style"])
    for i, (t, x, y, w, h) in enumerate(LABELS):
        text(f"t{i + 1}", t, x, y, w, h)

    return (
        '<mxfile host="skill-template">'
        '<diagram id="block-diagram" name="系统框图">'
        '<mxGraphModel dx="0" dy="0" grid="0" gridSize="10" guides="1" tooltips="1" connect="1" '
        'arrows="1" fold="1" page="1" pageScale="1" pageWidth="2600" pageHeight="1100" math="0" shadow="0">'
        '<root><mxCell id="0"/><mxCell id="1" parent="0"/>'
        + "".join(cells) +
        "</root></mxGraphModel></diagram></mxfile>"
    )


errs = selfcheck()
if errs:
    for msg in errs:
        print("[selfcheck] 问题:", msg)
    raise SystemExit(1)
with open(OUT, "w", encoding="utf-8") as f:
    f.write(render())
print("saved:", OUT, "| blocks:", len(BLOCKS), "| edges:", len(EDGES), "| labels:", len(LABELS))
