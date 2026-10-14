/**
 * NotificationBell -- 站内通知(审核结果等)。
 *
 * 容器负责 30s 轮询与已读回执;视图为纯渲染,便于静态测试。
 * 通知只是触达面,权威审计仍在修订表/日志中心。
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Bell } from 'lucide-react';

import { api } from '../api/client';
import { cn } from '@/lib/utils';

export type AppNotification = {
  notification_id: number;
  kind: string;
  target_type: string;
  target_id: string;
  title: string;
  body: string;
  payload: Record<string, unknown>;
  read: boolean;
  created_at: string;
};

type ViewProps = {
  unreadCount: number;
  items: AppNotification[];
  open: boolean;
  loading?: boolean;
  onToggle: () => void;
  onSelect: (item: AppNotification) => void;
  onMarkAll: () => void;
};

function formatTime(value: string): string {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleString('zh-CN', { hour12: false });
}

export function NotificationBellView({
  unreadCount,
  items,
  open,
  loading = false,
  onToggle,
  onSelect,
  onMarkAll,
}: ViewProps) {
  return (
    <div className="relative" data-notification-bell>
      <button
        type="button"
        aria-label={`通知${unreadCount > 0 ? `（${unreadCount} 条未读）` : ''}`}
        aria-expanded={open}
        onClick={onToggle}
        className="relative flex size-[32px] items-center justify-center rounded-full text-[#464c5e] transition-colors hover:bg-[#f1f3f8]"
      >
        <Bell className="size-[17px]" />
        {unreadCount > 0 && (
          <span
            data-notification-badge
            className="absolute -right-[2px] -top-[2px] min-w-[16px] rounded-full bg-[#d20b0b] px-[4px] text-center text-[10px] leading-[16px] font-medium text-white"
          >
            {unreadCount > 99 ? '99+' : unreadCount}
          </span>
        )}
      </button>
      {open && (
        <div
          data-notification-panel
          className="absolute right-0 top-[38px] z-50 w-[320px] overflow-hidden rounded-[12px] border border-[#e3e7f1] bg-white shadow-[0_12px_32px_rgba(24,24,26,0.12)]"
        >
          <div className="flex items-center justify-between border-b border-[#eef0f4] px-[12px] py-[8px]">
            <span className="text-[13px] font-medium text-[#18181a]">通知</span>
            {unreadCount > 0 && (
              <button
                type="button"
                onClick={onMarkAll}
                className="text-[12px] font-medium text-[#0b6cf5] hover:underline"
              >
                全部已读
              </button>
            )}
          </div>
          <div className="max-h-[360px] overflow-y-auto">
            {loading ? (
              <p className="px-[12px] py-[16px] text-[12px] text-[#858b9c]">加载中…</p>
            ) : items.length === 0 ? (
              <p className="px-[12px] py-[16px] text-[12px] text-[#858b9c]">暂无通知。</p>
            ) : (
              items.map((item) => (
                <button
                  key={item.notification_id}
                  type="button"
                  data-notification-id={item.notification_id}
                  onClick={() => onSelect(item)}
                  className={cn(
                    'flex w-full flex-col gap-[2px] border-b border-[#f4f5f8] px-[12px] py-[9px] text-left transition-colors hover:bg-[#f8fafc]',
                    !item.read && 'bg-[#f4f8ff]',
                  )}
                >
                  <span className="flex items-center gap-[6px] text-[13px] text-[#18181a]">
                    {!item.read && <span className="size-[6px] shrink-0 rounded-full bg-[#0b6cf5]" />}
                    <span className="min-w-0 truncate font-medium">{item.title}</span>
                  </span>
                  {item.body && (
                    <span className="text-[12px] text-[#68728a] [word-break:break-word]">{item.body}</span>
                  )}
                  <span className="text-[11px] text-[#9aa1b3]">{formatTime(item.created_at)}</span>
                </button>
              ))
            )}
          </div>
        </div>
      )}
    </div>
  );
}

export default function NotificationBell() {
  const navigate = useNavigate();
  const [items, setItems] = useState<AppNotification[]>([]);
  const [unreadCount, setUnreadCount] = useState(0);
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const rootRef = useRef<HTMLDivElement | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const data = await api.get<{ items: AppNotification[]; unread_count: number }>(
        '/api/v1/notifications?limit=20',
      );
      setItems(Array.isArray(data?.items) ? data.items : []);
      setUnreadCount(Number(data?.unread_count ?? 0));
    } catch {
      // Notifications are best-effort; a failed poll must stay silent.
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), 30_000);
    return () => window.clearInterval(timer);
  }, [load]);

  useEffect(() => {
    if (!open) return undefined;
    const onDocClick = (event: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(event.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener('mousedown', onDocClick);
    return () => document.removeEventListener('mousedown', onDocClick);
  }, [open]);

  const markRead = useCallback(async (item: AppNotification) => {
    if (item.read) return;
    try {
      await api.post(`/api/v1/notifications/${item.notification_id}/read`);
      setItems((prev) =>
        prev.map((row) =>
          row.notification_id === item.notification_id ? { ...row, read: true } : row,
        ),
      );
      setUnreadCount((prev) => Math.max(0, prev - 1));
    } catch {
      // Keep the unread state; the next poll reconciles.
    }
  }, []);

  const markAll = useCallback(async () => {
    try {
      await api.post('/api/v1/notifications/read-all');
      setItems((prev) => prev.map((row) => ({ ...row, read: true })));
      setUnreadCount(0);
    } catch {
      // no-op
    }
  }, []);

  return (
    <div ref={rootRef}>
      <NotificationBellView
        unreadCount={unreadCount}
        items={items}
        open={open}
        loading={loading}
        onToggle={() => setOpen((value) => !value)}
        onMarkAll={() => void markAll()}
        onSelect={(item) => {
          void markRead(item);
          if (item.target_type === 'attachment_revision') {
            navigate('/chat');
          } else if (item.target_type === 'quality_ticket') {
            navigate(`/admin/quality?ticket=${encodeURIComponent(item.target_id)}`);
            setOpen(false);
          } else if (item.target_type === 'quality_case') {
            navigate('/admin/quality');
            setOpen(false);
          }
        }}
      />
    </div>
  );
}
