import { useMemo } from 'react';

import type { WikiGraphView } from '../api/types';

const NODE_FILL: Record<string, string> = {
  summary: '#2b7a57',
  entity: '#3d5a91',
  concept: '#b45309',
  index: '#5b6478',
};

const TYPE_LABEL: Record<string, string> = {
  summary: '文档摘要',
  entity: '实体',
  concept: '概念',
  index: '索引',
};

type Props = {
  graph: WikiGraphView;
  onSelect: (slug: string) => void;
};

/** 零依赖 SVG 知识图谱: 中心为链接最多的枢纽, 其余按环形排布, 点击节点打开详情。 */
export default function WikiGraph({ graph, onSelect }: Props) {
  const layout = useMemo(() => {
    const ordered = [...graph.nodes].sort((a, b) => b.link_count - a.link_count);
    const pos = new Map<string, { x: number; y: number; r: number }>();
    const [center, ...rest] = ordered;
    if (!center) {
      return { pos, viewBox: '0 0 640 360', labeled: new Set<string>() };
    }
    pos.set(center.slug, { x: 0, y: 0, r: 11 + Math.min(center.link_count, 12) });
    let idx = 0;
    let ring = 1;
    while (idx < rest.length) {
      const capacity = 6 * ring + 4;
      const radius = 86 + ring * 64;
      const batch = rest.slice(idx, idx + capacity);
      batch.forEach((node, i) => {
        const angle = (2 * Math.PI * i) / batch.length - Math.PI / 2 + ring * 0.35;
        pos.set(node.slug, {
          x: radius * Math.cos(angle),
          y: radius * 0.8 * Math.sin(angle),
          r: 5 + Math.min(node.link_count, 8),
        });
      });
      idx += batch.length;
      ring += 1;
    }
    let minX = 0;
    let maxX = 0;
    let minY = 0;
    let maxY = 0;
    pos.forEach(({ x, y, r }) => {
      minX = Math.min(minX, x - r - 30);
      maxX = Math.max(maxX, x + r + 30);
      minY = Math.min(minY, y - r - 22);
      maxY = Math.max(maxY, y + r + 22);
    });
    const labeled = new Set(ordered.slice(0, 40).map((node) => node.slug));
    return { pos, viewBox: `${minX} ${minY} ${maxX - minX} ${maxY - minY}`, labeled };
  }, [graph]);

  if (graph.nodes.length === 0) {
    return <div className="border-y border-[#edf0f5] py-[56px] text-center text-[13px] text-[#858b9c]">暂无图谱节点;先生成 Wiki。</div>;
  }

  return (
    <div>
      <div className="mb-[10px] flex flex-wrap items-center gap-[12px] text-[11px] text-[#5b6478]">
        {Object.keys(TYPE_LABEL).map((type) => (
          <span key={type} className="inline-flex items-center gap-[5px]">
            <span className="inline-block size-[8px] rounded-full" style={{ background: NODE_FILL[type] }} />
            {TYPE_LABEL[type]}
          </span>
        ))}
        <span className="ml-auto">
          {graph.total} 个节点{graph.truncated ? '(仅显示前 200)' : ''} · 圆圈越大链接越多 · 箭头为引用方向 · 点击节点查看详情
        </span>
      </div>
      <div className="overflow-hidden rounded-[14px] border border-[#f2f3f7] bg-white">
        <svg viewBox={layout.viewBox} className="block h-auto max-h-[560px] w-full" role="img" aria-label="Wiki 知识图谱">
          <defs>
            <marker id="wiki-edge-arrow" viewBox="0 0 10 10" refX={8} refY={5} markerWidth={7} markerHeight={7} orient="auto-start-reverse">
              <path d="M 0 1 L 9 5 L 0 9 z" fill="#c3cad9" />
            </marker>
          </defs>
          {graph.edges.map((edge, index) => {
            const from = layout.pos.get(edge.source);
            const to = layout.pos.get(edge.target);
            if (!from || !to) return null;
            return <line key={index} x1={from.x} y1={from.y} x2={to.x} y2={to.y} stroke="#dfe4ee" strokeWidth={1} markerEnd="url(#wiki-edge-arrow)" />;
          })}
          {graph.nodes.map((node) => {
            const point = layout.pos.get(node.slug);
            if (!point) return null;
            const dimmed = node.link_count === 0;
            return (
              <g
                key={node.slug}
                opacity={dimmed ? 0.45 : 1}
                className="cursor-pointer"
                onClick={() => onSelect(node.slug)}
              >
                <title>{node.title}</title>
                <circle cx={point.x} cy={point.y} r={point.r} fill={NODE_FILL[node.page_type] ?? '#5b6478'} opacity={0.88} />
                {layout.labeled.has(node.slug) && (
                  <text x={point.x} y={point.y + point.r + 12} textAnchor="middle" fontSize={10} fill="#5b6478">
                    {(node.title || node.slug).slice(0, 9)}
                  </text>
                )}
              </g>
            );
          })}
        </svg>
      </div>
    </div>
  );
}
