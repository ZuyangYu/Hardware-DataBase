import { useCallback, useEffect, useState } from 'react';

import type { KbView } from '../api/types';
import {
  continueTask,
  createTask,
  downloadTaskArtifact,
  getTask,
  listTaskDefinitions,
  listTasks,
  stopTask,
  type TaskArtifactView,
  type TaskDefinitionView,
  type TaskView,
} from '../api/tasks';
import AppHeader from '@/components/AppHeader';
import { notify } from '@/components/ui/app-toast';

type Props = {
  kbs: KbView[];
};

const TERMINAL = new Set(['completed', 'failed', 'cancelled']);

const STATUS_LABEL: Record<string, string> = {
  queued: '排队中',
  running: '运行中',
  paused: '部分完成',
  completed: '已完成',
  failed: '失败',
  cancelled: '已取消',
};

function statusClass(status: string): string {
  if (status === 'completed') return 'bg-[#e8f7ee] text-[#28784f]';
  if (status === 'failed') return 'bg-[#fce7e7] text-[#b42323]';
  if (status === 'cancelled') return 'bg-[#eef1f7] text-[#68728a]';
  if (status === 'paused') return 'bg-[#fff5df] text-[#99620b]';
  return 'bg-[#eaf1ff] text-[#2b5cb8]';
}

function formatBytes(value: number): string {
  if (!Number.isFinite(value) || value <= 0) return '0 B';
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

function ArtifactRow({ artifact }: { artifact: TaskArtifactView }) {
  return (
    <div className="flex items-center justify-between gap-3 border-b border-[#eef1f7] py-[8px] last:border-b-0">
      <div className="min-w-0">
        <div className="truncate text-[13px] font-medium text-[#28324a]">{artifact.name}</div>
        <div className="text-[11px] text-[#7b849c]">
          v{artifact.version} · {artifact.kind} · {formatBytes(artifact.size_bytes)}
        </div>
      </div>
      <button
        type="button"
        className="shrink-0 rounded-[8px] border border-[#d8dfee] px-[10px] py-[5px] text-[12px] text-[#2b5cb8] hover:bg-[#f4f7fd]"
        onClick={() => {
          void downloadTaskArtifact(artifact).catch((error: unknown) => {
            notify.error(error instanceof Error ? error.message : '下载失败');
          });
        }}
      >
        下载
      </button>
    </div>
  );
}

export default function TasksPage({ kbs }: Props) {
  const [tasks, setTasks] = useState<TaskView[]>([]);
  const [definitions, setDefinitions] = useState<TaskDefinitionView[]>([]);
  const [selectedId, setSelectedId] = useState('');
  const [task, setTask] = useState<TaskView | null>(null);
  const [query, setQuery] = useState('');
  const [kbName, setKbName] = useState('__general__');
  const [definitionId, setDefinitionId] = useState('');
  const [followup, setFollowup] = useState('');
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);

  const loadTask = useCallback(async (taskId: string) => {
    try {
      const next = await getTask(taskId);
      setTask(next);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '加载任务失败');
    }
  }, []);

  const refreshList = useCallback(async () => {
    try {
      const data = await listTasks();
      setTasks(data.items);
      setSelectedId((current) => current || data.items[0]?.id || '');
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '加载任务列表失败');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refreshList();
    void listTaskDefinitions()
      .then((data) => setDefinitions(data.items))
      .catch(() => setDefinitions([]));
  }, [refreshList]);

  useEffect(() => {
    if (!selectedId) {
      setTask(null);
      return;
    }
    void loadTask(selectedId);
  }, [selectedId, loadTask]);

  useEffect(() => {
    if (!selectedId) return;
    if (task && TERMINAL.has(task.status)) return;
    const timer = window.setInterval(() => {
      void loadTask(selectedId);
    }, 2500);
    return () => window.clearInterval(timer);
  }, [selectedId, task?.status, loadTask, task]);

  const onCreate = async () => {
    const text = query.trim();
    if (!text || busy) return;
    setBusy(true);
    try {
      const result = await createTask({
        query: text,
        kb_name: kbName,
        definition_id: definitionId || undefined,
      });
      setQuery('');
      setSelectedId(result.task.id);
      setTask(result.task);
      await refreshList();
      notify.success('任务已创建');
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '创建任务失败');
    } finally {
      setBusy(false);
    }
  };

  const onContinue = async () => {
    const text = followup.trim();
    if (!task || !text || busy) return;
    setBusy(true);
    try {
      await continueTask(task.id, text);
      setFollowup('');
      await loadTask(task.id);
      await refreshList();
      notify.success('已提交，agent 会继续处理');
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '继续任务失败');
    } finally {
      setBusy(false);
    }
  };

  const onStop = async () => {
    if (!task || busy) return;
    setBusy(true);
    try {
      await stopTask(task.id);
      await loadTask(task.id);
      await refreshList();
      notify.success('已请求停止');
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '停止任务失败');
    } finally {
      setBusy(false);
    }
  };

  const turn = task?.current_turn;
  const canStop = task && ['queued', 'running', 'paused'].includes(task.status);

  return (
    <div className="px-[24px] py-[20px]">
      <AppHeader
        title="工作台"
        description="通用 agent：在工作区里读写文件、挂载知识库检索、产出可下载的交付物；任务可继续。"
      />

      <div className="mt-[20px] grid gap-[16px] lg:grid-cols-[340px_minmax(0,1fr)]">
        <section className="space-y-[12px]">
          <div className="rounded-[14px] border border-[#e3e7f1] bg-white p-[14px]">
            <div className="mb-[8px] text-[13px] font-semibold text-[#28324a]">新建任务</div>
            <textarea
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="例如：按 TCN2 网表整理关键器件清单，输出 5 列表格"
              className="h-[92px] w-full resize-none rounded-[10px] border border-[#dfe4ef] px-[10px] py-[8px] text-[13px] outline-none focus:border-[#9db4e0]"
            />
            <div className="mt-[8px] grid grid-cols-2 gap-[8px]">
              <select
                value={kbName}
                onChange={(event) => setKbName(event.target.value)}
                className="h-[34px] rounded-[9px] border border-[#dfe4ef] px-[8px] text-[12px] text-[#28324a]"
              >
                <option value="__general__">不挂知识库</option>
                {kbs.map((kb) => (
                  <option key={kb.name} value={kb.name}>
                    {kb.name}
                  </option>
                ))}
              </select>
              <select
                value={definitionId}
                onChange={(event) => setDefinitionId(event.target.value)}
                className="h-[34px] rounded-[9px] border border-[#dfe4ef] px-[8px] text-[12px] text-[#28324a]"
              >
                <option value="">自由规划</option>
                {definitions.map((definition) => (
                  <option key={definition.id} value={definition.id}>
                    {definition.title || definition.id}
                  </option>
                ))}
              </select>
            </div>
            <button
              type="button"
              disabled={busy || !query.trim()}
              onClick={() => void onCreate()}
              className="mt-[10px] w-full rounded-[9px] bg-[#2b5cb8] py-[8px] text-[13px] font-medium text-white disabled:cursor-not-allowed disabled:opacity-50"
            >
              {busy ? '提交中…' : '开始任务'}
            </button>
          </div>

          <div className="rounded-[14px] border border-[#e3e7f1] bg-white">
            <div className="border-b border-[#eef1f7] px-[14px] py-[10px] text-[13px] font-semibold text-[#28324a]">
              任务列表
            </div>
            {loading ? (
              <div className="px-[14px] py-[16px] text-[12px] text-[#7b849c]">加载中…</div>
            ) : tasks.length === 0 ? (
              <div className="px-[14px] py-[16px] text-[12px] text-[#7b849c]">还没有任务</div>
            ) : (
              <div className="max-h-[520px] overflow-auto">
                {tasks.map((item) => (
                  <button
                    key={item.id}
                    type="button"
                    onClick={() => setSelectedId(item.id)}
                    className={`block w-full border-b border-[#eef1f7] px-[14px] py-[10px] text-left last:border-b-0 ${
                      item.id === selectedId ? 'bg-[#f4f7fd]' : 'hover:bg-[#fafbfe]'
                    }`}
                  >
                    <div className="flex items-center justify-between gap-[8px]">
                      <span className="truncate text-[13px] font-medium text-[#28324a]">{item.title}</span>
                      <span className={`shrink-0 rounded-full px-[8px] py-[2px] text-[11px] ${statusClass(item.status)}`}>
                        {STATUS_LABEL[item.status] || item.status}
                      </span>
                    </div>
                    <div className="mt-[3px] truncate text-[11px] text-[#7b849c]">{item.query}</div>
                  </button>
                ))}
              </div>
            )}
          </div>
        </section>

        <section className="min-h-[420px] rounded-[14px] border border-[#e3e7f1] bg-white p-[16px]">
          {!task ? (
            <div className="flex h-full items-center justify-center text-[13px] text-[#7b849c]">
              选择或创建一个任务
            </div>
          ) : (
            <div className="space-y-[14px]">
              <div className="flex flex-wrap items-start justify-between gap-[10px]">
                <div>
                  <div className="text-[16px] font-semibold text-[#1f2a44]">{task.title}</div>
                  <div className="mt-[4px] text-[11px] text-[#7b849c]">
                    {task.kb_name === '__general__' ? '未挂载知识库' : `知识库：${task.kb_name}`}
                    {task.definition_id ? ` · 定义：${task.definition_id}` : ' · 自由规划'}
                  </div>
                </div>
                <div className="flex items-center gap-[8px]">
                  <span className={`rounded-full px-[10px] py-[3px] text-[12px] ${statusClass(task.status)}`}>
                    {STATUS_LABEL[task.status] || task.status}
                  </span>
                  {canStop ? (
                    <button
                      type="button"
                      onClick={() => void onStop()}
                      disabled={busy}
                      className="rounded-[8px] border border-[#e3b4b4] px-[10px] py-[5px] text-[12px] text-[#b42323] hover:bg-[#fdf3f3] disabled:opacity-50"
                    >
                      停止
                    </button>
                  ) : null}
                </div>
              </div>

              {task.plan.length > 0 ? (
                <div className="rounded-[10px] bg-[#f7f8fb] p-[10px]">
                  {task.plan.map((step, index) => (
                    <div key={step.id || index} className="text-[12px] text-[#4d5872]">
                      {step.status === 'done' ? '✓' : step.status === 'running' ? '◐' : '○'} {step.title}
                    </div>
                  ))}
                </div>
              ) : null}

              {turn ? (
                <div>
                  <div className="mb-[6px] text-[12px] font-medium text-[#4d5872]">本轮输出</div>
                  {turn.error_message ? (
                    <div className="mb-[6px] rounded-[8px] bg-[#fdf3f3] px-[10px] py-[8px] text-[12px] text-[#b42323]">
                      {turn.error_message}
                    </div>
                  ) : null}
                  <pre className="max-h-[340px] overflow-auto whitespace-pre-wrap rounded-[10px] bg-[#f7f8fb] p-[12px] text-[13px] leading-6 text-[#28324a]">
                    {turn.answer || (turn.status === 'streaming' ? '生成中…' : '暂无输出')}
                  </pre>
                </div>
              ) : null}

              <div>
                <div className="mb-[6px] text-[12px] font-medium text-[#4d5872]">
                  产物（{task.artifacts.length}）
                </div>
                <div className="rounded-[10px] border border-[#eef1f7] px-[10px]">
                  {task.artifacts.length === 0 ? (
                    <div className="py-[10px] text-[12px] text-[#7b849c]">暂无产物</div>
                  ) : (
                    task.artifacts.map((artifact) => (
                      <ArtifactRow key={artifact.id} artifact={artifact} />
                    ))
                  )}
                </div>
              </div>

              <div className="flex gap-[8px]">
                <textarea
                  value={followup}
                  onChange={(event) => setFollowup(event.target.value)}
                  placeholder="继续修改，例如：把第 3 行的厂家补上"
                  className="h-[52px] flex-1 resize-none rounded-[10px] border border-[#dfe4ef] px-[10px] py-[8px] text-[13px] outline-none focus:border-[#9db4e0]"
                />
                <button
                  type="button"
                  disabled={busy || !followup.trim()}
                  onClick={() => void onContinue()}
                  className="h-[52px] rounded-[10px] bg-[#2b5cb8] px-[14px] text-[13px] font-medium text-white disabled:cursor-not-allowed disabled:opacity-50"
                >
                  继续
                </button>
              </div>
            </div>
          )}
        </section>
      </div>
    </div>
  );
}
