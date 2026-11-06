# -*- coding: utf-8 -*-
"""_collision —— 线穿块/线贴块/悬空端点/平行线间距/跨板线未经连接器（02 §7）
豁免：zone 是容器（不作为障碍）；接线端 2px 内接块豁免；悬空端点三类合法情形见代码。
CLI 同 scan_issues。
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _geom as G

TOOL = "_collision"


def collect(d):
    issues = []
    # 线穿块（zone 容器豁免；接线端豁免）
    for e in d.edges:
        ends = [e.path[0], e.path[-1]]
        for blk in d.blocks:
            if blk.is_zone or blk.is_text:
                continue
            r = blk.rect
            if any(G.on_border(p, r, 2) for p in ends):
                continue
            if any(G.seg_crosses_rect(a, b, r) for a, b in e.segs):
                issues.append(f"[{TOOL}] 线穿块 {e.id}×{blk.id}")
    # 线贴块：平行 <19px 且并行 >30px（接线端豁免）
    for e in d.edges:
        ends = [e.path[0], e.path[-1]]
        for a, b in e.segs:
            mid = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
            for blk in d.blocks:
                if blk.is_zone or blk.is_text:
                    continue
                r = blk.rect
                if any(G.on_border(p, r, 2) for p in ends) or G.point_in_rect(mid, r):
                    continue
                if a[1] == b[1]:
                    for yb in (r[1], r[3]):
                        if abs(a[1] - yb) < 19 and G.overlap_len(a[0], b[0], r[0], r[2]) >= 30:
                            issues.append(f"[{TOOL}] 线贴块 {e.id} 贴 {blk.id} d={abs(a[1]-yb):.0f}px")
                elif a[0] == b[0]:
                    for xb in (r[0], r[2]):
                        if abs(a[0] - xb) < 19 and G.overlap_len(a[1], b[1], r[1], r[3]) >= 30:
                            issues.append(f"[{TOOL}] 线贴块 {e.id} 贴 {blk.id} d={abs(a[0]-xb):.0f}px")
    # 悬空端点（豁免：接块 2px / T 分叉 6px / 断线 ≤200px / 连接器引出 10px）
    for e in d.edges:
        for p in (e.path[0], e.path[-1]):
            ok = any(G.on_border(p, blk.rect, 2) for blk in d.blocks if not blk.is_text)
            if not ok:
                ok = any(G.pt_seg_dist(p, a, b) < 6 for e2 in d.edges if e2.id != e.id for a, b in e2.segs)
            if not ok:
                ok = (e.dst_id is None or e.src_id is None) and e.length <= 200
            if not ok:
                ok = any(G.rect_point_dist(c.rect, p) <= 10 for c in d.conns)
            if not ok:
                issues.append(f"[{TOOL}] 悬空端点 {e.id} ({p[0]:.0f},{p[1]:.0f})")
    # 平行线间距：<15px 且并行 >30px
    seen = set()
    for i, e1 in enumerate(d.edges):
        for e2 in d.edges[i + 1:]:
            for a, b in e1.segs:
                for c, dd in e2.segs:
                    if a[1] == b[1] and c[1] == dd[1]:
                        off, ov = abs(a[1] - c[1]), G.overlap_len(a[0], b[0], c[0], dd[0])
                    elif a[0] == b[0] and c[0] == dd[0]:
                        off, ov = abs(a[0] - c[0]), G.overlap_len(a[1], b[1], c[1], dd[1])
                    else:
                        continue
                    if off < 15 and ov >= 30 and (e1.id, e2.id) not in seen:
                        seen.add((e1.id, e2.id))
                        issues.append(f"[{TOOL}] 平行线间距 {off:.0f}px<15 {e1.id}/{e2.id}")
    # 跨板线未经连接器（两端块分属不同 zone，路径 10px 内无连接器）
    if len(d.zones) >= 2:
        for e in d.edges:
            if not (e.src_id and e.dst_id):
                continue
            b1, b2 = d.block(e.src_id), d.block(e.dst_id)
            if not b1 or not b2:
                continue
            z1, z2 = d.zone_of(b1), d.zone_of(b2)
            if not z1 or not z2 or z1.id == z2.id:
                continue
            if not any(G.rect_point_dist(c.rect, p) <= 10 for c in d.conns for p in e.path):
                issues.append(f"[{TOOL}] 跨板线未经连接器 {e.id} ({z1.id}→{z2.id})")
    return issues


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    args = ap.parse_args()
    issues = collect(G.Diagram(args.file))
    for line in issues:
        print(line)
    sys.exit(1 if issues else 0)


if __name__ == "__main__":
    main()
