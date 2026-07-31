import { useState, type CSSProperties, type FormEvent } from 'react';

import { api } from '../api/client';
import type { LoginResponse } from '../api/types';
import type { AuthSession } from '../auth';
import { notify } from '@/components/ui/app-toast';
import AppHeader from '@/components/AppHeader';
import BrandLogo from '@/components/BrandLogo';
import AppIcon from '@/components/AppIcon';

type GraphNode = {
  id: string;
  x: number;
  y: number;
  size: number;
  driftX: number;
  driftY: number;
  delay: number;
};

function makeSeededRandom(seed: number) {
  let value = seed;
  return () => {
    value = (value * 1664525 + 1013904223) % 4294967296;
    return value / 4294967296;
  };
}

function makeGraphNodes(): GraphNode[] {
  const random = makeSeededRandom(20260729);
  const clusters = [
    { cx: 24, cy: 35, count: 22, rx: 30, ry: 24 },
    { cx: 43, cy: 25, count: 26, rx: 38, ry: 26 },
    { cx: 58, cy: 42, count: 28, rx: 42, ry: 34 },
    { cx: 38, cy: 66, count: 20, rx: 38, ry: 26 },
    { cx: 76, cy: 31, count: 18, rx: 32, ry: 24 },
    { cx: 72, cy: 70, count: 16, rx: 30, ry: 24 },
    { cx: 52, cy: 78, count: 12, rx: 34, ry: 18 },
  ];

  return clusters.flatMap((cluster, clusterIndex) =>
    Array.from({ length: cluster.count }).map((_, index) => {
      const x = Math.max(6, Math.min(94, cluster.cx + (random() - 0.5) * cluster.rx));
      const y = Math.max(10, Math.min(86, cluster.cy + (random() - 0.5) * cluster.ry));
      const hot = random();
      return {
        id: `${clusterIndex}-${index}`,
        x,
        y,
        size: hot > 0.82 ? 4.2 : 3.2,
        driftX: Math.round((random() - 0.5) * 360),
        driftY: Math.round((random() - 0.5) * 240),
        delay: Number((-random() * 6.8).toFixed(2)),
      };
    }),
  );
}

function makeGraphEdges(nodes: GraphNode[]) {
  const edges: { from: GraphNode; to: GraphNode; delay: number }[] = [];
  nodes.forEach((node, index) => {
    const nearest = nodes
      .map((candidate, candidateIndex) => ({
        candidate,
        candidateIndex,
        distance: Math.hypot(node.x - candidate.x, node.y - candidate.y),
      }))
      .filter((item) => item.candidateIndex !== index)
      .sort((a, b) => a.distance - b.distance)
      .slice(0, index % 4 === 0 ? 6 : 4);

    nearest.forEach(({ candidate, candidateIndex }, nearestIndex) => {
      if (candidateIndex <= index) return;
      edges.push({ from: node, to: candidate, delay: (index + nearestIndex) * 0.04 });
    });
  });
  return edges.slice(0, 260);
}

const GRAPH_NODES = makeGraphNodes();
const GRAPH_EDGES = makeGraphEdges(GRAPH_NODES);

function DataFlowBackdrop() {
  return (
    <div className="login-data-flow" aria-hidden="true">
      <svg className="login-graph-lines" viewBox="0 0 100 100" preserveAspectRatio="none">
        {GRAPH_EDGES.map((edge, index) => (
          <line
            key={`${edge.from.id}-${edge.to.id}-${index}`}
            className="login-graph-line"
            x1={edge.from.x}
            y1={edge.from.y}
            x2={edge.to.x}
            y2={edge.to.y}
            pathLength={1}
            style={{ animationDelay: `${edge.delay}s` }}
          />
        ))}
      </svg>
      {GRAPH_NODES.map((node) => (
        <span
          key={node.id}
          className="login-graph-node"
          style={
            {
              top: `${node.y}%`,
              left: `${node.x}%`,
              width: node.size,
              height: node.size,
              '--drift-x': `${node.driftX}px`,
              '--drift-y': `${node.driftY}px`,
              animationDelay: `${node.delay}s`,
            } as CSSProperties
          }
        />
      ))}
    </div>
  );
}

/**
 * 登录页:全屏竖向 hero(顶 60px AppHeader 只放 BrandLogo -> 居中 pill 徽标
 * -> 大字两行 wordmark -> 黑色「登录」CTA,点击就地换成 320px 表单)。
 */
export default function LoginPage({ onLogin }: { onLogin: (session: AuthSession) => void }) {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [showPassword, setShowPassword] = useState(false);
  const [showForm, setShowForm] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [errorText, setErrorText] = useState('');

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    if (submitting) return;
    const name = username.trim();
    if (!name || !password) {
      setErrorText('请输入用户名和密码');
      return;
    }
    setErrorText('');
    setSubmitting(true);
    try {
      const resp = await api.post<LoginResponse>('/api/v1/login', { username: name, password });
      onLogin({ token: resp.token, user: resp.user });
      notify.success(`欢迎,${resp.user.username}`);
    } catch (error) {
      setErrorText(error instanceof Error ? error.message : '登录失败');
    } finally {
      setSubmitting(false);
    }
  }

  const inputClass =
    'h-[44px] w-full rounded-[10px] border bg-white px-[16px] text-[14px] text-[#18181a] outline-none transition-colors placeholder:text-[#b3b8c4] focus:border-[#18181a]';

  const featureCards = [
    {
      icon: 'database',
      title: '硬件数据管理',
      text: '统一管理知识库、文件和结构化资产，围绕设备台账与部门归属展开。',
    },
    {
      icon: 'grid',
      title: '结构化解析',
      text: '支持文档、表格、电路和原理图等结构化召回，不把问答放在主位。',
    },
    {
      icon: 'lock',
      title: '权限治理',
      text: '按部门挂载、授权、重挂，治理角色与内容访问边界分离。',
    },
    {
      icon: 'history',
      title: '审计追踪',
      text: '保留操作日志、查询日志和评估记录，方便回溯与排查。',
    },
  ] as const;

  return (
    <div className="relative flex min-h-screen flex-col overflow-hidden bg-[#f8fafb]">
      <DataFlowBackdrop />
      <AppHeader
        className="relative z-[1] h-[60px] items-center px-[32px]"
        left={<div className="pt-[3px]"><BrandLogo markSize={28} /></div>}
        right={null}
      />
      <main className="relative z-[1] flex flex-1 flex-col items-center px-[32px]">
        <div className={`flex w-full max-w-[1120px] flex-col items-center ${showForm ? 'pt-[56px]' : 'pt-[88px]'}`}>
          <span className="rounded-[10px] border-[0.5px] border-[#e3e7f1] bg-[#f6f6f6] px-[20px] py-[6px] text-[14px] text-[#464c5e]">
            硬件数据平台
          </span>
          <h1 className="mt-[6px] text-center text-[48px] font-semibold leading-[64px] tracking-[1px] text-[#18181a] max-[560px]:text-[34px] max-[560px]:leading-[48px]">
            Hardware DataBase
            <br />
            硬件数据平台
          </h1>

          {!showForm ? (
            <button
              type="button"
              onClick={() => setShowForm(true)}
              className="mt-[28px] rounded-[10px] bg-[#18181a] px-[36px] py-[10px] text-[16px] text-white transition-colors hover:bg-[#303030]"
            >
              登录
            </button>
          ) : (
            <form
              className="mt-[24px] flex w-[320px] flex-col duration-300 ease-out animate-in fade-in slide-in-from-top-4"
              onSubmit={handleSubmit}
            >
              <div className="relative">
                <input
                  value={username}
                  onChange={(e) => setUsername(e.target.value)}
                  placeholder="用户名"
                  autoComplete="username"
                  autoFocus
                  className={inputClass}
                />
                {username && (
                  <button
                    type="button"
                    aria-label="清除"
                    onClick={() => setUsername('')}
                    className="absolute right-[12px] top-1/2 -translate-y-1/2 text-[#b3b8c4] hover:text-[#18181a]"
                  >
                    <AppIcon name="close" size={16} />
                  </button>
                )}
              </div>
              <div className="relative mt-[16px]">
                <input
                  type={showPassword ? 'text' : 'password'}
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  placeholder="密码"
                  autoComplete="current-password"
                  className={inputClass}
                />
                <button
                  type="button"
                  aria-label={showPassword ? '隐藏密码' : '显示密码'}
                  onClick={() => setShowPassword((v) => !v)}
                  className="absolute right-[12px] top-1/2 -translate-y-1/2 text-[#b3b8c4] hover:text-[#18181a]"
                >
                  {showPassword ? <AppIcon name="eye-off" size={16} /> : <AppIcon name="eye" size={16} />}
                </button>
              </div>
              {errorText && (
                <p className="mt-[10px] text-[13px] text-[#d20b0b]">{errorText}</p>
              )}
              <button
                type="submit"
                disabled={submitting}
                className="mt-[20px] flex h-[40px] w-[120px] self-center items-center justify-center rounded-[10px] bg-[#18181a] text-[16px] text-white transition-colors hover:bg-[#303030] disabled:opacity-50"
              >
                {submitting ? '登录中…' : '登录'}
              </button>
            </form>
          )}
        </div>

        <section className="mt-[72px] w-full max-w-[1200px] pb-[56px] max-[720px]:mt-[56px]">
          <div className="grid gap-[16px] md:grid-cols-2 xl:grid-cols-4">
            {featureCards.map((card) => (
              <article
                key={card.title}
                className="group flex min-h-[190px] flex-col items-center justify-center rounded-[20px] border border-[#e6e9f1] bg-white/90 p-[24px] shadow-[0_12px_32px_rgba(17,17,17,0.07)] backdrop-blur-[8px] transition-transform duration-200 hover:-translate-y-[3px]"
              >
                <div className="flex w-full max-w-[230px] flex-col items-center">
                  <div className="mb-[14px] flex size-[42px] items-center justify-center rounded-[14px] bg-[#f6f6f6] text-[#18181a]">
                    <AppIcon name={card.icon as Parameters<typeof AppIcon>[0]['name']} size={20} />
                  </div>
                  <h3 className="text-center text-[16px] font-semibold text-[#18181a]">{card.title}</h3>
                  <p className="mt-[10px] self-stretch text-left text-[13px] leading-[20px] text-[#757f9c]">{card.text}</p>
                </div>
              </article>
            ))}
          </div>
        </section>

        <footer className="relative z-[1] mt-auto flex items-center justify-center pb-[28px]">
          <a
            href="https://github.com/ZuyangYu/Hardware-DataBase"
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-[7px] rounded-[10px] px-[14px] py-[8px] text-[13px] text-[#757f9c] transition-colors hover:bg-[#f0f0f0] hover:text-[#18181a]"
          >
            <AppIcon name="github" size={18} />
            <span>GitHub</span>
          </a>
        </footer>
      </main>
    </div>
  );
}
