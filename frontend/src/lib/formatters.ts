/** 管理工作台共享格式化:相对时间、绝对时间、数值/未知值。
 *  统一来源,避免各页面自行 toLocaleString 造成语义漂移。 */

import type { StatusTone } from '@/components/admin/StatusBadge';

/** 调用/任务状态徽章色调(颜色仅辅助,文案始终存在)。 */
const STATUS_TONE: Record<string, StatusTone> = {
  success: 'success', succeeded: 'success', complete: 'success', completed: 'success', ok: 'success',
  failed: 'danger', error: 'danger', timeout: 'danger',
  pending: 'warning', started: 'info', running: 'info', unfinished: 'warning', unknown: 'neutral',
};
export function statusTone(status: string): StatusTone {
  return STATUS_TONE[status] ?? 'neutral';
}

/** 相对时间(中文)。无法解析或为空返回 '—',不假装成 0 或 "刚刚"。 */
export function formatRelativeTime(iso: string | null | undefined, now: Date = new Date()): string {
  if (!iso) return '—';
  const then = new Date(iso);
  if (Number.isNaN(then.getTime())) return '—';
  const difference = now.getTime() - then.getTime();
  const abs = Math.abs(difference);
  const MINUTE = 60_000;
  const HOUR = 3_600_000;
  const DAY = 86_400_000;
  const suffix = difference < 0 ? '后' : '前';
  if (abs < MINUTE) return difference < 0 ? '即将' : '刚刚';
  if (abs < HOUR) return `${Math.floor(abs / MINUTE)} 分钟${suffix}`;
  if (abs < DAY) return `${Math.floor(abs / HOUR)} 小时${suffix}`;
  if (abs < 30 * DAY) return `${Math.floor(abs / DAY)} 天${suffix}`;
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

/** 多币种金额:"USD 1.00 · CNY 2.00";空值为明确中文而非空白。 */
export function formatMultiCurrencyAmount(values: Record<string, string>): string {
  return Object.entries(values).map(([currency, n]) => `${currency} ${n}`).join(' · ') || '无已知金额';
}

/** 百分比:null/undefined 为 '—',与 0% 区分。 */
export function formatPercent(value: number | null | undefined, digits = 1): string {
  if (value == null || !Number.isFinite(value)) return '—';
  return `${(value * 100).toFixed(digits)}%`;
}

/** Backend decimal ratio, not an amount. Null/blank must not turn into a false 0%. */
export function decimalRatio(value: string | null | undefined): number | null {
  if (value == null || !value.trim()) return null;
  const ratio = Number(value);
  return Number.isFinite(ratio) && ratio >= 0 ? ratio : null;
}
