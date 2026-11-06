# -*- coding: utf-8 -*-
"""_audit —— 三向语义审计：孤儿标签 / 缺标注线（红线）/ 幽灵线（02 §7）
距离统一用标签 bbox 边缘到线段路径的距离；阈值：孤儿>80px、缺标注/幽灵>60px。
红线（电源线）走「缺标注线」，非红线走「幽灵线」。CLI 同 scan_issues。
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _geom as G

TOOL = "_audit"


def collect(d):
    issues = []
    # 孤儿标签：距任何线 >80px
    for lb in d.labels:
        best = min((G.label_path_dist(lb.rect, e) for e in d.edges), default=float("inf"))
        if best > 80:
            issues.append(f"[{TOOL}] 孤儿标签 {lb.id}「{lb.value[:12]}」距最近线 {best:.0f}px")
    # 缺标注线（红线）/ 幽灵线（黑线）：60px 内无标签
    for e in d.edges:
        if getattr(e, "value", "").strip():
            continue  # drawio inline edge label；文字已随线，不参与 60px 邻近判定
        best = min((G.label_path_dist(lb.rect, e) for lb in d.labels), default=float("inf"))
        if e.red and best > 60:
            issues.append(f"[{TOOL}] 缺标注线 {e.id} 红线 60px 内无标签")
        elif not e.red and best > 60:
            issues.append(f"[{TOOL}] 幽灵线 {e.id} 60px 内无任何标签")
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
