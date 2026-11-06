# -*- coding: utf-8 -*-
"""_tcheck —— 文字-连线干涉 / 文字跨框（02 §7）
距离用标签 bbox 边缘；干涉阈值 <3px。CLI 同 scan_issues。
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _geom as G

TOOL = "_tcheck"


def collect(d):
    issues = []
    # 文字-连线干涉：标签 bbox 边缘距任一线段 <3px
    for lb in d.labels:
        r = lb.rect
        for e in d.edges:
            if G.label_path_dist(r, e) < 3:
                issues.append(f"[{TOOL}] 文字-连线干涉 {lb.id}×{e.id}「{lb.value[:12]}」")
    # 文字跨框：标签 bbox 与块 bbox 相交且标签中心在块外
    for lb in d.labels:
        r = lb.rect
        c = lb.center
        for blk in d.blocks:
            if blk.is_text or blk.is_zone:
                continue
            br = blk.rect
            overlap = not (r[2] <= br[0] or br[2] <= r[0] or r[3] <= br[1] or br[3] <= r[1])
            if overlap and not (br[0] <= c[0] <= br[2] and br[1] <= c[1] <= br[3]):
                issues.append(f"[{TOOL}] 文字跨框 {lb.id}×{blk.id}「{lb.value[:12]}」")
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
