# -*- coding: utf-8 -*-
"""scan_text_block —— 文字-文字重叠/间隙<8px、块伸出 zone 边、块内文字居中（02 §7）
CLI 同 scan_issues。
"""
import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _geom as G

TOOL = "scan_text_block"


def collect(d):
    issues = []
    # 文字-文字：重叠或间隙 <8px
    lbs = d.labels
    for i in range(len(lbs)):
        for j in range(i + 1, len(lbs)):
            dist = G.rect_rect_dist(lbs[i].rect, lbs[j].rect)
            if dist < 8:
                issues.append(
                    f"[{TOOL}] 文字-文字重叠/间隙<8 {lbs[i].id}/{lbs[j].id} ({dist:.0f}px)")
    # 块伸出 zone 边（块中心在 zone 内才判）
    for blk in d.blocks:
        if blk.is_zone or blk.is_text:
            continue
        z = d.zone_of(blk)
        if not z:
            continue
        bx0, by0, bx1, by1 = blk.rect
        zx0, zy0, zx1, zy1 = z.rect
        if bx0 < zx0 - 0.5 or by0 < zy0 - 0.5 or bx1 > zx1 + 0.5 or by1 > zy1 + 0.5:
            issues.append(f"[{TOOL}] 块伸出 zone 边 {blk.id}→{z.id}")
    # 块内文字居中（显式 horizontal=left 即违规）
    for blk in d.blocks:
        if blk.is_zone or blk.is_text:
            continue
        if re.search(r"horizontal\s*=\s*left", blk.style, re.I):
            issues.append(f"[{TOOL}] 块内文字未居中 {blk.id}")
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
