import type { CostTrends } from '@/api/workbenchTypes';
import { formatMultiCurrencyAmount as amount, formatPercent as percentage } from '@/lib/formatters';

type Bucket = CostTrends['buckets'][number];

function labelForBucket(value: string, bucket: CostTrends['bucket']): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value.slice(0, 16);
  return date.toLocaleString('zh-CN', bucket === 'day'
    ? { month: 'numeric', day: 'numeric', timeZone: 'UTC' }
    : { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', timeZone: 'UTC', hour12: false });
}

function numericAmount(value: string | undefined): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed >= 0 ? parsed : 0;
}

function currencyValues(buckets: Bucket[], fallback: Record<string, string> = {}): string[] {
  const values = new Set(Object.keys(fallback));
  buckets.forEach((item) => Object.keys(item.known_amounts).forEach((currency) => values.add(currency)));
  return [...values].sort();
}

function TrendCurrencyPanel({ currency, buckets, bucket }: { currency: string; buckets: Bucket[]; bucket: CostTrends['bucket'] }) {
  const visibleBuckets = buckets.slice(-48);
  const values = visibleBuckets.map((item) => numericAmount(item.known_amounts[currency]));
  const max = Math.max(...values, 0);
  const latest = visibleBuckets.length ? visibleBuckets[visibleBuckets.length - 1].known_amounts[currency] : undefined;

  return (
    <div className="min-w-0 rounded-lg border bg-muted/10 p-3" aria-label={`${currency}费用趋势`}>
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h3 className="font-medium">{currency}</h3>
        <span className="text-xs text-muted-foreground">最近时间桶：{latest == null ? '—' : `${currency} ${latest}`}</span>
      </div>
      <div className="mt-3 grid gap-2" role="img" aria-label={`${currency}已知费用柱状趋势，时间按 UTC 显示`}>
        {visibleBuckets.map((item) => {
          const value = item.known_amounts[currency];
          const width = max > 0 ? (numericAmount(value) / max) * 100 : 0;
          return (
            <div className="grid grid-cols-[72px_minmax(0,1fr)_auto] items-center gap-2 text-xs" key={item.bucket_start}>
              <span className="text-muted-foreground">{labelForBucket(item.bucket_start, bucket)}</span>
              <div className="h-2 overflow-hidden rounded-full bg-muted" title={value == null ? '该时间桶没有已知金额' : `${currency} ${value}`}>
                <div className="h-full rounded-full bg-primary/70" style={{ width: `${width}%` }} />
              </div>
              <span className="min-w-[5rem] text-right font-mono">{value == null ? '—' : `${currency} ${value}`}</span>
            </div>
          );
        })}
        {!visibleBuckets.length && <p className="py-6 text-center text-sm text-muted-foreground">当前范围暂无已知费用趋势</p>}
      </div>
      {buckets.length > visibleBuckets.length && (
        <p className="mt-3 text-xs text-muted-foreground">图表显示最近 {visibleBuckets.length} 个时间桶；完整调用仍可在下方明细和 CSV 中查看。</p>
      )}
    </div>
  );
}

export function CostTrendChart({ trends }: { trends: CostTrends | null }) {
  if (!trends) return <p className="rounded-lg border border-dashed p-6 text-center text-sm text-muted-foreground">趋势数据尚未加载</p>;
  const currencies = currencyValues(trends.buckets, trends.summary.known_amounts);
  if (!currencies.length) return <p className="rounded-lg border border-dashed p-6 text-center text-sm text-muted-foreground">当前范围没有已知金额，未知费用不会被绘制为零。</p>;

  return (
    <div className="grid gap-3 xl:grid-cols-2">
      {currencies.map((currency) => <TrendCurrencyPanel key={currency} currency={currency} buckets={trends.buckets} bucket={trends.bucket} />)}
    </div>
  );
}

export function TrendSummary({ trends }: { trends: CostTrends | null }) {
  if (!trends) return null;
  return (
    <div className="flex flex-wrap gap-x-5 gap-y-1 text-xs text-muted-foreground">
      <span>{trends.summary.calls} 次调用</span>
      <span>已知费用：{amount(trends.summary.known_amounts)}</span>
      <span>未知费用：{trends.summary.unknown_cost_calls} 次</span>
      <span>计价覆盖：{percentage(trends.summary.price_coverage)}</span>
      <span>空桶省略，不推定为零</span>
    </div>
  );
}
