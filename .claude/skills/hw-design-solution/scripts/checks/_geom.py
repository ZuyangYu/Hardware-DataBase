# -*- coding: utf-8 -*-
"""drawio 几何共享库 —— scripts/checks 六个检测工具共用
- 解析：未压缩 XML 与 drawio 压缩格式（base64 + raw deflate + URL 解码）都支持
- 节点坐标沿 parent 链累加为绝对坐标；边路径 = sourcePoint + <Array as="points"> + targetPoint
- 边端点：source/target id 存在时按 exit/entry 样式解析，否则贴块边最近点；id 缺失用坐标（断线画法）
- 分类：zone=w>1000 且 h>1000；连接器=value 含 连接器/CONN；红线=style 含 strokeColor ff0000；
        独立文字标签=style 以 text; 开头；字号双来源 style fontSize= / value font-size:NNpx
- 距离一律用几何距离（bbox 边缘/线段），不用中心点
"""
import base64
import math
import re
import zlib
import xml.etree.ElementTree as ET
from urllib.parse import unquote


def load_root(path):
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    try:
        tree = ET.fromstring(text)
    except ET.ParseError:
        m = re.search(r"<diagram[^>]*>([^<]+)</diagram>", text)
        if not m:
            raise
        raw = base64.b64decode(m.group(1).strip())
        xml_text = unquote(zlib.decompress(raw, -15).decode("utf-8", "replace"))
        tree = ET.fromstring(xml_text)
    if tree.tag.split("}")[-1] == "mxGraphModel":
        model = tree
    else:
        model = tree.find(".//mxGraphModel")
    if model is None:
        raise ValueError("未找到 mxGraphModel")
    return model.find("root")


class Block:
    def __init__(self, bid, x, y, w, h, value, style):
        self.id = bid
        self.x, self.y, self.w, self.h = x, y, w, h
        self.value = value
        self.style = style
        self.is_text = style.strip().startswith("text;")
        self.is_zone = (not self.is_text) and w > 1000 and h > 1000
        self.is_conn = (not self.is_text) and (("连接器" in value) or ("CONN" in value.upper()))

    @property
    def rect(self):
        return (self.x, self.y, self.x + self.w, self.y + self.h)

    @property
    def center(self):
        return (self.x + self.w / 2, self.y + self.h / 2)


class Edge:
    def __init__(self, eid, path, style, src_id, dst_id, value=""):
        self.id = eid
        self.value = value or ""
        self.path = path
        self.style = style
        self.src_id = src_id
        self.dst_id = dst_id
        self.segs = list(zip(path, path[1:]))
        self.red = "ff0000" in style.lower()
        self.jump = "jumpStyle" in style
        m = re.search(r"endArrow\s*=\s*(\w+)", style)
        self.end_arrow = bool(m) and m.group(1).lower() != "none"
        m = re.search(r"startArrow\s*=\s*(\w+)", style)
        self.start_arrow = bool(m) and m.group(1).lower() != "none"
        self.length = sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in self.segs)


class Diagram:
    def __init__(self, path):
        root = load_root(path)
        raw = {}
        for el in root.findall("mxCell"):
            cid = el.get("id")
            if cid in (None, "0", "1"):
                continue
            raw[cid] = el
        # 先块后边：边的端点锚定依赖块索引
        self.blocks = []
        for cid, el in raw.items():
            if el.get("vertex") == "1":
                b = self._vertex(cid, el, el.get("style") or "", raw)
                if b:
                    self.blocks.append(b)
        self._by_id = {b.id: b for b in self.blocks}
        self.edges = []
        for cid, el in raw.items():
            if el.get("edge") == "1":
                e = self._edge(cid, el, el.get("style") or "", raw)
                if e:
                    self.edges.append(e)
        self.labels = [b for b in self.blocks if b.is_text]
        self.zones = [b for b in self.blocks if b.is_zone]
        self.conns = [b for b in self.blocks if b.is_conn]

    # ---- 解析 ----
    def _abs_xy(self, cid, raw, memo):
        if cid in memo:
            return memo[cid]
        el = raw.get(cid)
        if el is None:
            memo[cid] = (0.0, 0.0)
            return memo[cid]
        g = el.find("mxGeometry")
        px = float(g.get("x") or 0) if g is not None else 0.0
        py = float(g.get("y") or 0) if g is not None else 0.0
        par = el.get("parent")
        if par in (None, "0", "1"):
            memo[cid] = (px, py)
        else:
            ox, oy = self._abs_xy(par, raw, memo)
            memo[cid] = (px + ox, py + oy)
        return memo[cid]

    def _vertex(self, cid, el, style, raw):
        g = el.find("mxGeometry")
        if g is None or g.get("x") is None or g.get("y") is None:
            return None  # 相对几何（edge label 等）不当块处理
        ox, oy = self._abs_xy(el.get("parent"), raw, {})
        x = ox + float(g.get("x") or 0)
        y = oy + float(g.get("y") or 0)
        w = float(g.get("width") or 0)
        h = float(g.get("height") or 0)
        return Block(cid, x, y, w, h, el.get("value") or "", style)

    def _edge(self, cid, el, style, raw):
        g = el.find("mxGeometry")
        sp = tp = None
        wps = []
        if g is not None:
            for child in g:
                tag = child.tag.split("}")[-1]
                if tag == "mxPoint":
                    p = (float(child.get("x") or 0), float(child.get("y") or 0))
                    as_ = child.get("as")
                    if as_ == "sourcePoint":
                        sp = p
                    elif as_ == "targetPoint":
                        tp = p
                elif tag == "Array" and child.get("as") == "points":
                    for pt in child:
                        if pt.tag.split("}")[-1] == "mxPoint":
                            wps.append((float(pt.get("x") or 0), float(pt.get("y") or 0)))
        src_id, dst_id = el.get("source"), el.get("target")
        if src_id and src_id in self._by_id:
            other = tp if tp is not None else (wps[0] if wps else None)
            sp = self._anchor(self._by_id[src_id], style, other, "exit")
        if dst_id and dst_id in self._by_id:
            other = sp if sp is not None else (wps[-1] if wps else None)
            tp = self._anchor(self._by_id[dst_id], style, other, "entry")
        path = [p for p in ([sp] + wps + [tp]) if p is not None]
        if len(path) < 2:
            return None
        return Edge(cid, path, style, src_id, dst_id, el.get("value") or "")

    def _anchor(self, b, style, other, kind):
        m = re.search(rf"{kind}X=([\d.]+).*?{kind}Y=([\d.]+)", style)
        if m:
            return (b.x + float(m.group(1)) * b.w, b.y + float(m.group(2)) * b.h)
        # 无 exit/entry：取块边上朝对端最近的点
        cx, cy = b.center
        dx = (other[0] - cx) if other else 0.0
        dy = (other[1] - cy) if other else 1.0
        if dx == 0 and dy == 0:
            dy = 1.0
        scale = min((b.w / 2) / abs(dx) if dx else float("inf"), (b.h / 2) / abs(dy) if dy else float("inf"))
        return (cx + dx * scale, cy + dy * scale)

    # ---- 查询 ----
    def block(self, bid):
        return self._by_id.get(bid)

    def zone_of(self, b):
        """块中心落在哪个 zone 内（用于跨板归属）"""
        for z in self.zones:
            if z.x <= b.x + b.w / 2 <= z.x + z.w and z.y <= b.y + b.h / 2 <= z.y + z.h:
                return z
        return None


# ---- 几何 ----
def pt_seg_dist(p, a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    l2 = dx * dx + dy * dy
    if l2 == 0:
        return math.hypot(p[0] - a[0], p[1] - a[1])
    t = max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / l2))
    return math.hypot(p[0] - a[0] - t * dx, p[1] - a[1] - t * dy)


def _orient(a, b, c):
    v = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
    return 0 if abs(v) < 1e-9 else (1 if v > 0 else -1)


def _on_seg(a, b, p):
    return (min(a[0], b[0]) - 1e-9 <= p[0] <= max(a[0], b[0]) + 1e-9
            and min(a[1], b[1]) - 1e-9 <= p[1] <= max(a[1], b[1]) + 1e-9)


def segs_cross(a, b, c, d):
    """线段严格相交（共点/共线不算）"""
    o1, o2 = _orient(a, b, c), _orient(a, b, d)
    o3, o4 = _orient(c, d, a), _orient(c, d, b)
    return o1 != o2 and o3 != o4 and o1 != 0 and o2 != 0 and o3 != 0 and o4 != 0


def seg_seg_dist(a, b, c, d):
    if segs_cross(a, b, c, d):
        return 0.0
    return min(pt_seg_dist(a, c, d), pt_seg_dist(b, c, d), pt_seg_dist(c, a, b), pt_seg_dist(d, a, b))


def rect_seg_dist(rect, a, b):
    x0, y0, x1, y1 = rect
    edges = [((x0, y0), (x1, y0)), ((x1, y0), (x1, y1)), ((x1, y1), (x0, y1)), ((x0, y1), (x0, y0))]
    return min(seg_seg_dist(a, b, s, t) for s, t in edges)


def rect_point_dist(rect, p):
    x0, y0, x1, y1 = rect
    edges = [((x0, y0), (x1, y0)), ((x1, y0), (x1, y1)), ((x1, y1), (x0, y1)), ((x0, y1), (x0, y0))]
    return min(pt_seg_dist(p, s, t) for s, t in edges)


def rect_rect_dist(r1, r2):
    ax0, ay0, ax1, ay1 = r1
    bx0, by0, bx1, by1 = r2
    dx = max(bx0 - ax1, ax0 - bx1, 0.0)
    dy = max(by0 - ay1, ay0 - by1, 0.0)
    return math.hypot(dx, dy)


def point_in_rect(p, rect):
    x0, y0, x1, y1 = rect
    return x0 < p[0] < x1 and y0 < p[1] < y1


def on_border(p, rect, tol=2.0):
    if point_in_rect(p, rect):
        return False
    return rect_point_dist(rect, p) <= tol


def path_len(path):
    return sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(path, path[1:]))


def label_path_dist(rect, edge):
    """标签 bbox 到整条边路径的最小边缘距离"""
    return min(rect_seg_dist(rect, a, b) for a, b in edge.segs) if edge.segs else float("inf")


def seg_crosses_rect(a, b, rect):
    """线段是否穿入矩形内部（端点在内/跨界两次；贴边不算）"""
    if point_in_rect(a, rect) or point_in_rect(b, rect):
        return True
    x0, y0, x1, y1 = rect
    crosses = 0
    for s, t in (((x0, y0), (x1, y0)), ((x1, y0), (x1, y1)), ((x1, y1), (x0, y1)), ((x0, y1), (x0, y0))):
        if segs_cross(a, b, s, t):
            crosses += 1
    return crosses >= 2


def overlap_len(a, b, c, d):
    """两条同轴平行段的共轴重叠长度"""
    return max(0.0, min(b, d) - max(a, c))


def font_size(style, value):
    m = re.search(r"fontSize\s*=\s*(\d+)", style, re.I)
    if m:
        return int(m.group(1))
    m = re.search(r"font-size\s*:\s*(\d+)\s*px", value or "", re.I)
    if m:
        return int(m.group(1))
    return 12
