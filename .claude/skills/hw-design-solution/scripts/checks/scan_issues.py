# -*- coding: utf-8 -*-
"""scan_issues —— 斜线/板框重合/块缘平行贴行/端点贴角/字号审计/紧凑断口对（02 §7）
CLI: python scan_issues.py <file.drawio>；问题逐行打印，干净无输出；exit 0=干净 1=有问题
"""
import argparse
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _geom as G

TOOL = "scan_issues"


def collect(d):
    issues = []
    # 1 斜线：相邻路径点 dx·dy≠0
    for e in d.edges:
        for a, b in e.segs:
            if a[0] != b[0] and a[1] != b[1]:
                issues.append(f"[{TOOL}] 斜线 {e.id} ({a[0]:.0f},{a[1]:.0f})→({b[0]:.0f},{b[1]:.0f})")
    # 2 板框重合：线段与 zone 边共线 ≤2px 且搭接 ≥30px
    for e in d.edges:
        for a, b in e.segs:
            if a[1] == b[1]:
                for z in d.zones:
                    for yb in (z.y, z.y + z.h):
                        if abs(a[1] - yb) <= 2 and G.overlap_len(a[0], b[0], z.x, z.x + z.w) >= 30:
                            issues.append(f"[{TOOL}] 板框重合 {e.id} 与 zone {z.id} 边共线 y={a[1]:.0f}")
            elif a[0] == b[0]:
                for z in d.zones:
                    for xb in (z.x, z.x + z.w):
                        if abs(a[0] - xb) <= 2 and G.overlap_len(a[1], b[1], z.y, z.y + z.h) >= 30:
                            issues.append(f"[{TOOL}] 板框重合 {e.id} 与 zone {z.id} 边共线 x={a[0]:.0f}")
    # 3 块缘平行贴行：与普通块边平行 <19px 且并行 >30px（接线端豁免）
    for e in d.edges:
        ends = [e.path[0], e.path[-1]]
        for a, b in e.segs:
            mid = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
            for blk in d.blocks:
                if blk.is_zone or blk.is_text:
                    continue
                r = blk.rect
                if any(G.on_border(p, r, 2) for p in ends):
                    continue
                if G.point_in_rect(mid, r):
                    continue
                if a[1] == b[1]:
                    for yb in (r[1], r[3]):
                        if abs(a[1] - yb) < 19 and G.overlap_len(a[0], b[0], r[0], r[2]) >= 30:
                            issues.append(f"[{TOOL}] 块缘平行贴行 {e.id} 贴 {blk.id} d={abs(a[1]-yb):.0f}px")
                elif a[0] == b[0]:
                    for xb in (r[0], r[2]):
                        if abs(a[0] - xb) < 19 and G.overlap_len(a[1], b[1], r[1], r[3]) >= 30:
                            issues.append(f"[{TOOL}] 块缘平行贴行 {e.id} 贴 {blk.id} d={abs(a[0]-xb):.0f}px")
    # 4 端点贴角：端点在块角 10px 盒内但未接块
    for e in d.edges:
        for p in (e.path[0], e.path[-1]):
            for blk in d.blocks:
                if blk.is_text:
                    continue
                r = blk.rect
                if G.on_border(p, r, 2) or G.point_in_rect(p, r):
                    continue
                for cx, cy in ((r[0], r[1]), (r[2], r[1]), (r[2], r[3]), (r[0], r[3])):
                    if abs(p[0] - cx) <= 10 and abs(p[1] - cy) <= 10:
                        issues.append(f"[{TOOL}] 端点贴角 {e.id} ({p[0]:.0f},{p[1]:.0f}) 贴 {blk.id} 角")
                        break
    # 5 字号审计：独立标签基准 fs25（style/value 双来源）
    for lb in d.labels:
        fs = G.font_size(lb.style, lb.value)
        if fs != 25:
            issues.append(f"[{TOOL}] 字号非基准 fs{fs} {lb.id} 「{lb.value[:12]}」")
    # 6 紧凑断口对：相向箭头端 <40px
    tips = []
    for e in d.edges:
        if e.end_arrow and len(e.path) >= 2:
            tips.append((e, e.path[-1], (e.path[-1][0] - e.path[-2][0], e.path[-1][1] - e.path[-2][1])))
        if e.start_arrow and len(e.path) >= 2:
            tips.append((e, e.path[0], (e.path[0][0] - e.path[1][0], e.path[0][1] - e.path[1][1])))
    for i in range(len(tips)):
        for j in range(i + 1, len(tips)):
            e1, p1, d1 = tips[i]
            e2, p2, d2 = tips[j]
            if e1.id == e2.id:
                continue
            dist = math.hypot(p2[0] - p1[0], p2[1] - p1[1])
            facing = (p2[0] - p1[0]) * d1[0] + (p2[1] - p1[1]) * d1[1] > 0 \
                and (p1[0] - p2[0]) * d2[0] + (p1[1] - p2[1]) * d2[1] > 0
            if dist < 40 and facing:
                issues.append(f"[{TOOL}] 紧凑断口对 {e1.id}/{e2.id} 相距 {dist:.0f}px<40")
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
