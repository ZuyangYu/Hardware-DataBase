import { describe, expect, it } from 'vitest';

import {
  formatAbsoluteTime,
  formatMultiCurrencyAmount,
  formatPercent,
  formatRelativeTime,
  formatUnknown,
  statusTone,
} from './formatters';

describe('formatRelativeTime', () => {
  const now = new Date('2026-10-09T12:00:00Z');

  it('一分钟内显示刚刚', () => {
    expect(formatRelativeTime('2026-10-09T11:59:30Z', now)).toBe('刚刚');
  });

  it('按分钟/小时/天分档', () => {
    expect(formatRelativeTime('2026-10-09T11:30:00Z', now)).toBe('30 分钟前');
    expect(formatRelativeTime('2026-10-09T05:00:00Z', now)).toBe('7 小时前');
    expect(formatRelativeTime('2026-10-07T12:00:00Z', now)).toBe('2 天前');
  });

  it('超过 30 天回落为日期,长时间文本截断为日期而非巨大数字', () => {
    expect(formatRelativeTime('2026-01-01T12:00:00Z', now)).toBe('2026/1/1');
  });

  it('空值/非法值显示 — 而不是 0 或刚刚', () => {
    expect(formatRelativeTime(null, now)).toBe('—');
    expect(formatRelativeTime(undefined, now)).toBe('—');
    expect(formatRelativeTime('not-a-date', now)).toBe('—');
  });

  it('未来时间不会误标为过去', () => {
    expect(formatRelativeTime('2026-10-09T12:30:00Z', now)).toBe('30 分钟后');
  });
});

describe('formatAbsoluteTime', () => {
  it('空值/非法值显示 —', () => {
    expect(formatAbsoluteTime(null)).toBe('—');
    expect(formatAbsoluteTime('bad')).toBe('—');
  });
});

describe('管理工作台共享格式化', () => {
  it('金额、百分比和状态色调保持未知值语义', () => {
    expect(formatMultiCurrencyAmount({ USD: '1.00', CNY: '2.00' })).toBe('USD 1.00 · CNY 2.00');
    expect(formatMultiCurrencyAmount({})).toBe('无已知金额');
    expect(formatPercent(null)).toBe('—');
    expect(formatPercent(0)).toBe('0.0%');
    expect(statusTone('error')).toBe('danger');
    expect(statusTone('unmapped')).toBe('neutral');
  });
});

describe('formatUnknown', () => {
  it('null/undefined 显示 —,0 保持 0', () => {
    expect(formatUnknown(null, (v) => String(v))).toBe('—');
    expect(formatUnknown(undefined, (v) => String(v))).toBe('—');
    expect(formatUnknown(0, (v) => String(v))).toBe('0');
    expect(formatUnknown(12, (v) => `${v}`)).toBe('12');
  });
});
