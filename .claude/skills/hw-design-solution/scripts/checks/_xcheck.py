# -*- coding: utf-8 -*-
"""_xcheck —— 黑线交叉必须 jumpStyle=arc（红线豁免；共享端点/贴角不算交叉）（02 §7）
CLI 同 scan_issues。
"""
import argparse
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _geom as G

TOOL = "_xcheck"


def _isect(a, b, c, d):
    x1, y1 = a
    x2, y2 = b
    x3, y3 = c
    x4, y4 = d
    den = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4)
    if abs(den) < 1e-9:
        return None
    t = ((x1 - x3) * (y3 - y4) - (y1 - y3) * (x3 - x4)) / den
    return (x1 + t * (x2 - x1), y1 + t * (y2 - y1))


def collect(d):
    issues = []
    seen = set()
    for i, e1 in enumerate(d.edges):
        for e2 in d.edges[i + 1:]:
            if e1.red or e2.red or e1.jump or e2.jump:
                continue
            hit = None
            for a, b in e1.segs:
                for c, dd in e2.segs:
                    if not G.segs_cross(a, b, c, dd):
                        continue
                    pt = _isect(a, b, c, dd)
                    if pt is None:
                        continue
                    # 交点落在某条线段端点（共享拐角）附近不算交叉
                    if min(math.hypot(pt[0] - p[0], pt[1] - p[1]) for p in (a, b, c, dd)) < 3:
                        continue
                    hit = pt
                    break
                if hit:
                    break
            if hit and (e1.id, e2.id) not in seen:
                seen.add((e1.id, e2.id))
                issues.append(
                    f"[{TOOL}] 线交叉无 jumpStyle {e1.id}/{e2.id} @({hit[0]:.0f},{hit[1]:.0f})")
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
