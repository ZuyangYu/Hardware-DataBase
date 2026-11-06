# -*- coding: utf-8 -*-
"""③ 电源树生成模板 v1 —— L1 树 drawio（规则见 references/03-power-calc.md 电源树部分）
数据驱动：轨拓扑注册表=唯一数据源（KL30 → 防反 → 一级 buck → 二级 buck → 负载），改数据不重写逻辑。
- 电源线一律红色正交线；二级轨→负载用断线画法（段A/段B，电压名=配对唯一依据）
- 每段线 60px 内必须有电压标签；独立标签 fontsize=25，禁骑线
输出：_out/demo_power_tree.drawio（HW_DESIGN_OUT 可覆盖目录）
"""
import html
import math
import os

OUT_DIR = os.environ.get("HW_DESIGN_OUT") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "_out")
os.makedirs(OUT_DIR, exist_ok=True)
OUT = os.path.join(OUT_DIR, "demo_power_tree.drawio")


def B(name, x, y, w, h, label="", style=""):
    return {"name": name, "x": x, "y": y, "w": w, "h": h, "label": label, "style": style}


def P(block, side, pos):
    return {"block": block, "side": side, "pos": pos}


# ── 样式 ──
S_BLK = "rounded=0;fillColor=#DAE8FC;strokeColor=#6C8EBF;fontSize=13;whiteSpace=wrap;"
S_IN = "rounded=0;fillColor=#FFE6CC;strokeColor=#D79B00;fontSize=13;whiteSpace=wrap;"
S_PWR = "edgeStyle=orthogonalEdgeStyle;rounded=0;strokeColor=#FF0000;strokeWidth=2;endArrow=block;endFill=1;"
S_TEXT = "text;html=1;align=center;verticalAlign=middle;fontSize=25;fontColor=#000000;whiteSpace=wrap;"

# ── 轨拓扑注册表（唯一数据源） ──
BLOCKS = [
    B("kl30",  500, 60,  260, 90,  "KL30 入口\n（接插件）", S_IN),
    B("d1",    530, 220, 200, 80,  "D1 防反二极管", S_BLK),
    B("buck6", 480, 380, 300, 110, "一级 buck 6V\nSA24506E", S_BLK),
    B("b33",   180, 620, 240, 100, "buck 3.3V 主轨\nSA25903", S_BLK),
    B("b18",   510, 620, 240, 100, "buck 1.8V\nSA25903", S_BLK),
    B("b09",   820, 620, 240, 100, "buck 0.9V NPU\nSA23004", S_BLK),
]
EDGES = [
    {"src": P("kl30", "B", 130),  "dst": P("d1", "T", 100),    "pts": [], "style": S_PWR, "pair": "9V 入口"},
    {"src": P("d1", "B", 100),    "dst": P("buck6", "T", 150), "pts": [], "style": S_PWR, "pair": "6V 总线"},
    {"src": P("buck6", "B", 80),  "dst": P("b33", "T", 120),   "pts": [(300, 490)], "style": S_PWR, "pair": "3.3V 轨"},
    {"src": P("buck6", "B", 150), "dst": P("b18", "T", 120),   "pts": [], "style": S_PWR, "pair": "1.8V 轨"},
    {"src": P("buck6", "B", 220), "dst": P("b09", "T", 120),   "pts": [(940, 490)], "style": S_PWR, "pair": "0.9V 轨"},
    # 段A×3：二级轨引出（尾部悬空），负载名+电流由标签承载
    {"src": P("b33", "B", 120), "dst": None, "pts": [(300, 810)], "style": S_PWR, "pair": "3.3V"},
    {"src": P("b18", "B", 120), "dst": None, "pts": [(630, 810)], "style": S_PWR, "pair": "1.8V"},
    {"src": P("b09", "B", 120), "dst": None, "pts": [(940, 810)], "style": S_PWR, "pair": "0.9V"},
]
LABELS = [
    ("9V 入口",                  660, 160, 130, 40),
    ("6V 总线",                  660, 310, 130, 40),
    ("3.3V 轨",                  150, 530, 110, 40),
    ("1.8V 轨",                  660, 530, 110, 40),
    ("0.9V 轨",                  970, 530, 110, 40),
    ("3.3V 主轨负载 292/404mA",  160, 850, 300, 40),
    ("1.8V 负载（摄像头域）",    520, 850, 300, 40),
    ("0.9V 负载 NPU 4000mA 底线", 880, 850, 300, 40),
]

BY_NAME = {b["name"]: b for b in BLOCKS}


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
    raise ValueError(f"未知边 {s}")


def resolve_edge(e):
    pts = list(e["pts"])
    sp = endpoint_pt(e["src"]) if e["src"] else None
    tp = endpoint_pt(e["dst"]) if e["dst"] else None
    if sp and tp:
        return sp, tp, pts
    if sp:
        return sp, pts[-1], pts[:-1]
    return pts[0], tp, pts[1:]


def _seg_rect_dist(a, b, r):
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
                errs.append(f"斜线 {e['pair']}")
        if not e["src"] or not e["dst"]:
            L = sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(path, path[1:]))
            if L > 200:
                errs.append(f"断线超长 {L:.0f}px：{e['pair']}")
    for text, x, y, w, h in LABELS:
        r = {"x": x, "y": y, "w": w, "h": h}
        for e in EDGES:
            sp, tp, wps = resolve_edge(e)
            path = [sp] + wps + [tp]
            d = min(_seg_rect_dist(a, b, r) for a, b in zip(path, path[1:]))
            if d < 25:
                errs.append(f"标签距线 {d:.0f}px <25：{text}")
    return errs


def esc(s):
    return html.escape(s, quote=True)


def render():
    cells = []
    for i, b in enumerate(BLOCKS):
        cells.append(
            f'<mxCell id="b{i + 1}" value="{esc(b["label"])}" style="{b["style"]}" vertex="1" parent="1">'
            f'<mxGeometry x="{b["x"]}" y="{b["y"]}" width="{b["w"]}" height="{b["h"]}" as="geometry"/></mxCell>')
    for i, e in enumerate(EDGES):
        sp, tp, wps = resolve_edge(e)
        arr = ""
        if wps:
            pts = "".join(f'<mxPoint x="{x}" y="{y}"/>' for x, y in wps)
            arr = f'<Array as="points">{pts}</Array>'
        cells.append(
            f'<mxCell id="e{i + 1}" style="{e["style"]}" edge="1" parent="1">'
            f'<mxGeometry relative="1" as="geometry">'
            f'<mxPoint x="{sp[0]}" y="{sp[1]}" as="sourcePoint"/>'
            f'<mxPoint x="{tp[0]}" y="{tp[1]}" as="targetPoint"/>{arr}</mxGeometry></mxCell>')
    for i, (t, x, y, w, h) in enumerate(LABELS):
        cells.append(
            f'<mxCell id="t{i + 1}" value="{esc(t)}" style="{S_TEXT}" vertex="1" parent="1">'
            f'<mxGeometry x="{x}" y="{y}" width="{w}" height="{h}" as="geometry"/></mxCell>')
    return (
        '<mxfile host="skill-template">'
        '<diagram id="power-tree" name="L1 电源树">'
        '<mxGraphModel dx="0" dy="0" grid="0" gridSize="10" guides="1" tooltips="1" connect="1" '
        'arrows="1" fold="1" page="1" pageScale="1" pageWidth="1400" pageHeight="1000" math="0" shadow="0">'
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
print("saved:", OUT, "| rails:", 3, "| edges:", len(EDGES), "| labels:", len(LABELS))
