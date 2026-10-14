/** Display contracts from costs/store.py.
 * Amounts and utilization are decimal strings; never recompute billing in the UI.
 * Kept separate from types.ts so existing in-flight API type changes stay untouched.
 */
export type CostInvocation = {
  invocation_id: string;
  task_type: string;
  task_id: string | null;
  purpose: string;
  started_at: string;
  requested_model: string;
  response_model: string;
  endpoint_alias: string;
  model_alias: string;
  status: string;
  input_tokens: number | null;
  output_tokens: number | null;
  cached_input_tokens: number | null;
  reasoning_tokens: number | null;
  usage_status: 'reported' | 'partial' | 'missing';
  cost_status: 'priced' | 'unknown_usage' | 'unknown_price';
  cost_amount: string | null;
  currency: string | null;
  attempt_visibility: string;
  price_version: string | null;
};

export type CostSummary = {
  calls: number;
  known_amounts: Record<string, string>;
  tokens: {
    input_tokens: number;
    output_tokens: number;
    cached_input_tokens: number;
    reasoning_tokens: number;
  };
  reported_usage_calls?: number;
  priced_calls?: number;
  unknown_cost_calls: number;
  usage_coverage: number | null;
  price_coverage: number | null;
  logical_only_calls: number;
  unfinished_calls: number;
  unattributed_calls: number;
  updated_at: string | null;
  scope: string;
  amount_kind?: 'application_token_estimate' | string;
  settlement_status?: 'not_reconciled' | string;
  by_purpose: Record<string, { calls: number; known_amounts: Record<string, string> }>;
};

export type CostTrends = {
  bucket: 'day' | 'hour';
  timezone: 'UTC';
  summary: CostSummary;
  buckets: (CostSummary & { bucket_start: string })[];
  models?: (CostSummary & {
    endpoint_alias: string;
    model_alias: string;
    requested_model: string;
    response_model: string;
  })[];
  rankings_by_currency: Record<string, {
    endpoint_alias: string;
    model_alias: string;
    requested_model: string;
    response_model: string;
    known_amount: string;
    calls: number;
    price_coverage: number | null;
  }[]>;
};

export type CostTask = {
  summary: CostSummary;
  invocations: CostInvocation[];
  limit: number;
  offset: number;
};

export type CostBudget = {
  policy: {
    budget_version: string;
    department_id: number | null;
    purpose: string | null;
    model_alias: string | null;
    currency: string;
    amount_limit: string;
    effective_from: string;
    effective_to: string;
  };
  active: boolean;
  known_amount: string;
  known_utilization: string;
  status: string;
  period_status: 'future' | 'active' | 'ended';
  summary: CostSummary;
};
