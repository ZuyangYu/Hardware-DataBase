import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';

import type { CostTrends } from '@/api/workbenchTypes';
import { CostTrendChart } from './CostTrendChart';

const baseSummary = {
  calls: 2,
  known_amounts: { USD: '1.20' },
  tokens: { input_tokens: 10, output_tokens: 20, cached_input_tokens: 0, reasoning_tokens: 0 },
  unknown_cost_calls: 1,
  usage_coverage: 0.5,
  price_coverage: 0.5,
  logical_only_calls: 0,
  unfinished_calls: 0,
  unattributed_calls: 0,
  updated_at: null,
  scope: 'global',
  by_purpose: {},
};

describe('CostTrendChart', () => {
  it('keeps unknown amounts distinct from zero', () => {
    const trends: CostTrends = {
      bucket: 'day',
      timezone: 'UTC',
      summary: baseSummary,
      buckets: [{ ...baseSummary, bucket_start: '2026-10-10T00:00:00+00:00', known_amounts: { USD: '1.20' } }],
      rankings_by_currency: {},
    };
    const markup = renderToStaticMarkup(<CostTrendChart trends={trends} />);
    expect(markup).toContain('USD 1.20');
    expect(markup).not.toContain('USD 0');
  });

  it('renders a separate panel for each currency', () => {
    const trends: CostTrends = {
      bucket: 'hour',
      timezone: 'UTC',
      summary: { ...baseSummary, known_amounts: { CNY: '2.00', USD: '1.20' } },
      buckets: [{ ...baseSummary, bucket_start: '2026-10-10T08:00:00+00:00', known_amounts: { CNY: '2.00', USD: '1.20' } }],
      rankings_by_currency: {},
    };
    const markup = renderToStaticMarkup(<CostTrendChart trends={trends} />);
    expect(markup).toContain('aria-label="CNY费用趋势"');
    expect(markup).toContain('aria-label="USD费用趋势"');
  });
});
