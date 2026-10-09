/** 管理工作台共享格式化:相对时间、绝对时间、数值/未知值。
 *  统一来源,避免各页面自行 toLocaleString 造成语义漂移。 */

/** 相对时间(中文)。无法解析或为空返回 '—',不假装成 0 或 "刚刚"。 */
export function formatRelativeTime(iso: string | null | undefined, now: Date = new Date()): string {
  if (!iso) return '—';
  const then = new Date(iso);
  if (Number.isNaN(then.getTime())) return '—';
  const abs = Math.abs(now.getTime() - then.getTime());
  const MINUTE = 60_000;
  const HOUR = 3_600_000;
  const DAY = 86_400_000;
  if (abs < MINUTE) return '刚刚';
  if (abs < HOUR) return `${Math.floor(abs / MINUTE)} 分钟前`;
  if (abs < DAY) return `${Math.floor(abs / HOUR)} 小时前`;
  if (abs < 30 * DAY) return `${Math.floor(abs / DAY)} 天前`;
  return then.toLocaleDateString('zh-CN');
}

/** 绝对时间(title/详情用),保留浏览器区域设置的可读格式。 */
export function formatAbsoluteTime(iso: string | null | undefined): string {
  if (!iso) return '—';
  const then = new Date(iso);
  if (Number.isNaN(then.getTime())) return '—';
  return then.toLocaleString('zh-CN');
}

/** 未知/缺失数值显示为 '—',与数值 0 区分。 */
export function formatUnknown(value: number | string | null | undefined, format: (v: number | string) => string): string {
  if (value == null) return '—';
  const formatted = format(value);
  return formatted === '' || formatted === 'NaN' ? '—' : formatted;
}
