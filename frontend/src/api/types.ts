/** 后端 /api/v1 的 DTO 镜像(对齐 src/api/schemas.py) */

export type Role = 'system_admin' | 'dept_admin' | 'user';

export interface UserInfo {
  username: string;
  role: Role;
  department_id: number | null;
  department_name: string | null;
}

export interface LoginResponse {
  token: string;
  user: UserInfo;
}

export interface OkResponse {
  ok: boolean;
  message: string;
}

export interface KbView {
  name: string;
  kb_id: number | null;
  department_id: number | null;
  department_name: string | null;
  permission: string | null;
  registered: boolean;
}

export interface FileView {
  id: string;
  name: string;
  status: string;
  processor_kind: string;
  dataset_kind: string;
  metadata: Record<string, unknown>;
}

export interface UploadAck {
  success_count: number;
  total_count: number;
  failed_count: number;
  skipped_count: number;
  status: string;
  messages: string[];
}

export interface AssetEvidenceView {
  id: number;
  file_id: string;
  file_name: string;
  locator: string;
  excerpt: string;
  metadata: Record<string, unknown>;
  created_at: string;
}

export interface AssetView {
  id: number;
  department_id: number;
  kb_id: number;
  asset_type: 'device' | 'board' | 'component' | 'firmware' | 'other';
  name: string;
  model: string;
  manufacturer: string;
  serial_number: string;
  version: string;
  status: string;
  owner_user_id: number | null;
  attributes: Record<string, unknown>;
  evidence_count: number;
  created_at: string;
  updated_at: string;
}

export interface AssetDetailView extends AssetView {
  evidence: AssetEvidenceView[];
}

export interface AssetCandidateView {
  id: number;
  kb_name: string;
  file_id: string;
  file_name: string;
  source_kind: string;
  extraction_method: 'llm' | 'rule';
  asset_type: AssetView['asset_type'];
  name: string;
  model: string;
  manufacturer: string;
  version: string;
  attributes: Record<string, unknown>;
  evidence_excerpt: string;
  evidence_locator: string;
  confidence: number;
  status: 'pending' | 'accepted' | 'rejected';
  asset_id: number | null;
  created_at: string;
  resolved_at: string | null;
}

export interface AssetSourceLinkView {
  file_id: string;
  file_name: string;
  file_status: string;
  processor_kind: string;
  dataset_kind: string;
  link_status: 'unprocessed' | 'pending_review' | 'linked' | 'ignored';
  candidate_id: number | null;
  asset_id: number | null;
  asset_name: string;
  source_category: 'circuit_design' | 'structured_table' | 'hardware_requirement' | 'hardware_architecture' | 'document_rag';
  extraction_target: string;
  asset_eligible: boolean;
}

export interface SessionView {
  id: number;
  user_id: number;
  kb_name: string;
  title: string;
  created_at: string;
  updated_at: string;
}

export interface MessageView {
  id: number;
  session_id: number;
  role: 'user' | 'assistant' | 'system';
  content: string;
  footer?: string;
  created_at: string;
}

export interface TurnView {
  id: string;
  session_id: number;
  user_message_id: number;
  assistant_message_id: number;
  kb_name: string;
  query: string;
  query_mode: 'fast' | 'deep';
  status: 'pending' | 'streaming' | 'cancelling' | 'completed' | 'cancelled' | 'failed';
  cancel_requested: boolean;
  last_event_seq: number;
  answer: string;
  summary: QueryDonePayload['summary'];
  footer: string;
  metrics: Record<string, unknown>;
  error_message: string;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface TurnStartResponse {
  turn: TurnView;
  user_message: MessageView;
}

export interface ChunkView {
  index: number;
  content: string;
  metadata: Record<string, unknown>;
}

export interface ParseResultView {
  document_id: string;
  file_name: string;
  chunk_count: number;
  chunks: ChunkView[];
  backend: string;
}

export interface ParseTaskView {
  id: string;
  kb_name: string;
  source_path: string;
  original_name: string;
  source_group: string;
  created_by: string;
  status: string;
  progress: number;
  stage: string;
  message: string;
  result: string;
  document_id: string;
  created_at?: number | null;
  updated_at?: number | null;
  started_at?: number | null;
  finished_at?: number | null;
}

// ---- 知识库结构化数据 ----

export interface SpreadsheetLedgerRow {
  file_id: string;
  file_name: string;
  status: string;
  status_label: string;
  sheet_count: number;
  row_count: number;
  cell_count: number;
  semantic_row_count: number;
  block_count: number;
  object_count: number;
  record_id: string;
  kb_id: string;
  archive_path: string;
  sheets: Record<string, unknown>[];
}

export interface SpreadsheetLedgerResponse {
  totals: {
    file_count?: number;
    sheet_count?: number;
    semantic_row_count?: number;
    pending_count?: number;
    [key: string]: unknown;
  };
  rows: SpreadsheetLedgerRow[];
}

export interface CircuitDesignRow {
  design_id: string;
  status: string;
  files: string[];
  instance_count: number;
  net_count: number;
  module_count: number;
}

export interface CircuitDesignsResponse {
  designs: CircuitDesignRow[];
  failed_logs: { design_id: string; log_path: string }[];
}

export interface CircuitDesignDetailResponse {
  summary: Record<string, unknown>;
  modules: Record<string, unknown>[];
  nets: Record<string, unknown>[];
  instances: Record<string, unknown>[];
  cross_references: Record<string, unknown>[];
}

export interface CircuitParseLogResponse {
  exists: boolean;
  path: string;
  size: number;
  truncated: boolean;
  content: string;
}

export interface StructuredRowsResponse<T = Record<string, unknown>> {
  rows: T[];
}

export interface SchematicDesignRow {
  design_id: string;
  status: string;
  page_count: number;
  label_count: number;
  module_region_count: number;
  pages: {
    page_number: number;
    width?: number | null;
    height?: number | null;
    label_count: number;
    text_preview: string;
  }[];
}

export interface SchematicDesignsResponse {
  designs: SchematicDesignRow[];
}

export interface SchematicPageResponse {
  design_id: string;
  page_number: number;
  width?: number | null;
  height?: number | null;
  text: string;
  labels: Record<string, unknown>[];
  module_regions: Record<string, unknown>[];
  screenshots: string[];
  pdf_cache: string[];
}

/** POST /query 的 SSE done 事件里的 retrieval summary(evidence 结构宽松,逐字段防御式读取) */
export interface EvidenceItem {
  file_name?: string;
  source_name?: string;
  document_id?: string;
  chunk_id?: string;
  score?: number;
  text?: string;
  text_preview?: string;
  source_type?: string;
  content_kind?: string;
  content?: string;
  locator?: Record<string, unknown>;
  metadata?: Record<string, unknown>;
  [key: string]: unknown;
}

export interface QueryDonePayload {
  answer: string;
  summary?: {
    rewritten_queries?: string[];
    retriever_type?: string;
    final_top_k?: number;
    evidence?: EvidenceItem[];
    [key: string]: unknown;
  } | null;
  footer?: string;
  token_usage?: unknown;
}

export type QueryTraceStatus = 'pending' | 'running' | 'done' | 'error';

export interface QueryTraceStep {
  key: string;
  label: string;
  status: QueryTraceStatus;
  detail?: string;
}

// ---- 管理:用户/部门/KB 授权(对齐 src/api/schemas.py) ----

export interface UserView {
  id: number;
  username: string;
  role: Role;
  is_active: boolean;
  department_id?: number | null;
  department_name?: string | null;
}

export interface DepartmentView {
  id: number;
  name: string;
}

export interface KbPermissionView {
  username: string;
  role: Role;
  permission: string;
  department_name?: string | null;
}

export interface CreateUserPayload {
  username: string;
  password: string;
  role: Role;
  department_id?: number | null;
}

export interface CreateDepartmentPayload {
  name: string;
}

export interface GrantKbPermissionPayload {
  user_id: number;
  permission: 'read' | 'write' | 'admin';
}

export interface AssignKbPayload {
  department_id: number;
  owner_user_id?: number | null;
  source_kb_id?: number | null;
}

// ---- 治理/日志/配置(对齐 src/api/schemas.py) ----

export interface KbSummaryView {
  name: string;
  kb_id?: number | null;
  department_id?: number | null;
  department_name?: string | null;
  owner_user_id?: number | null;
  owner_username?: string | null;
  permission_count: number;
  dept_admin_count: number;
  registered: boolean;
  physical_exists: boolean;
  created_at: string;
  files: number;
  failed: number;
  parsing: number;
  issue_flags: string[];
}

export interface AuditEventView {
  id: number;
  actor_user_id?: number | null;
  actor_username: string;
  actor_role: string;
  department_id?: number | null;
  action: string;
  target_type: string;
  target_id: string;
  kb_name: string;
  success: boolean;
  error_message: string;
  metadata_json: string;
  created_at: string;
}

export interface AuditStatsResponse {
  total: number;
  breakdown: Record<string, number>;
  actions: [string, number][];
  daily: [string, number][];
}

export interface QueryTraceView {
  id: number;
  username: string;
  department_id?: number | null;
  chat_session_id?: number | null;
  user_message_id?: number | null;
  assistant_message_id?: number | null;
  kb_name: string;
  original_query: string;
  rewritten_query: string;
  backend: string;
  retriever_type: string;
  final_top_k?: number | null;
  latency_ms?: number | null;
  status: string;
  error_message: string;
  metadata_json: string;
  created_at: string;
  otel_trace_id: string;
  otel_span_id: string;
  turn_id: string;
  grafana_trace_url: string;
  phoenix_trace_url: string;
}

export interface QueryStatsResponse {
  total: number;
  breakdown: Record<string, number>;
  failures: [string, number][];
}

export interface EvidenceView {
  id: number;
  trace_id: number;
  rank: number;
  file_name: string;
  document_id: string;
  chunk_id: string;
  vector_score?: number | null;
  bm25_score?: number | null;
  rrf_score?: number | null;
  rerank_score?: number | null;
  text_preview: string;
  metadata_json: string;
  created_at: string;
}

export interface ConfigResponse {
  settings: Record<string, string | number | boolean | null>;
}

export interface RagflowHealthResponse {
  reachable: boolean;
  message: string;
  missing_datasets: string[];
}

export interface LlmHealthResponse {
  reachable: boolean;
  message: string;
  provider: string;
}

// ---- RAGAS 评估(system_admin) ----

export type EvaluationMode = 'online' | 'offline';
export type EvaluationRunStatus =
  | 'queued'
  | 'running'
  | 'pause_requested'
  | 'paused'
  | 'cancel_requested'
  | 'cancelled'
  | 'completed'
  | 'failed'
  | 'invalid';
export type EvaluationRunStage = 'idle' | 'collecting' | 'scoring' | 'reporting';

export interface EvaluationRunListItem {
  run_id: string;
  status: EvaluationRunStatus | '';
  has_summary: boolean;
}

export interface CreateEvaluationRunPayload {
  dataset_path: string;
  mode: EvaluationMode;
  score_enabled: boolean;
  sample_ids?: string[] | null;
  tags?: string[] | null;
  snapshot_path?: string | null;
}

export interface EvaluationGateResult {
  passed: boolean;
  exit_code: number;
  metric_scores: Record<string, number>;
  metric_counts: Record<string, number>;
  failures: string[];
}

export interface EvaluationSummary {
  run_id: string;
  created_at: string;
  sample_count: number;
  successful_samples: number;
  failed_samples: number;
  metric_scores: Record<string, number>;
  metric_counts: Record<string, number>;
  metric_failures: Record<string, number>;
  scoring_completed_items: number;
  scoring_total_items: number;
  gate?: EvaluationGateResult | null;
  metadata: Record<string, unknown>;
}

export interface EvaluationMetricResult {
  sample_id: string;
  metric_name: string;
  score: number | null;
  status: 'success' | 'failed' | 'not_applicable';
  reason: string;
  details: Record<string, unknown>;
}

export interface EvaluationSampleResult {
  sample_id: string;
  question: string;
  reference_answer: string;
  response: string;
  scored_response: string;
  retrieved_contexts: string[];
  critical: boolean;
  snapshot_status: 'success' | 'failed';
  metrics: EvaluationMetricResult[];
  metadata: Record<string, unknown>;
}

export interface EvaluationRunDetail {
  run_id: string;
  dataset_path: string;
  snapshot_path: string;
  mode: EvaluationMode;
  score_enabled: boolean;
  sample_ids: string[];
  tags: string[];
  status: EvaluationRunStatus;
  stage: EvaluationRunStage;
  total_samples: number;
  completed_samples: number;
  successful_samples: number;
  failed_samples: number;
  scoring_completed_groups: number;
  scoring_total_groups: number;
  scoring_completed_items: number;
  scoring_total_items: number;
  current_sample_id: string;
  current_question: string;
  started_at: string;
  updated_at: string;
  finished_at: string;
  error_message: string;
  report_path: string;
  summary?: EvaluationSummary | null;
  sample_results: EvaluationSampleResult[];
  sample_results_error: string;
}

export interface EvaluationCompareResponse {
  current: EvaluationSummary;
  baseline: EvaluationSummary;
}

export interface EvaluationDatasetUploadResponse {
  dataset_path: string;
  file_name: string;
  sample_count: number;
}

export type DocumentUnit = {
  unit_id: string;
  label: string;
  writable: boolean;
  blocked_reason?: string | null;
};

export type DocumentSuggestion = {
  semantic_unit_id: string;
  label: string;
  confidence: number;
};

export type DocumentAnalysis = {
  analysis_id: string;
  template_version_id: string;
  format: string;
  status: string;
  units: DocumentUnit[];
  suggestions: DocumentSuggestion[];
  reason_codes?: string[];
  auto_activated?: boolean;
};

export type TemplateReviewUnit = DocumentUnit & {
  structural_role_hint: string;
  candidate_for_auto_fill: boolean;
};

export type TemplateReviewSuggestion = DocumentSuggestion & {
  target_unit_ids: string[];
  retrieval_terms: string[];
  value_shape: 'scalar' | 'repeating_table';
  overwrite_basis: 'placeholder' | 'sample_value' | null;
};

export type TemplateAnalysisReview = {
  analysis_id: string;
  template_version_id: string;
  content_hash: string;
  format: string;
  status: string;
  units: TemplateReviewUnit[];
  suggestions: TemplateReviewSuggestion[];
  locked_unit_ids: string[];
  reason_codes: string[];
};

export type TemplateCorrectionRequest = {
  expected_content_hash: string;
  selected_suggestion_ids: string[];
  locked_unit_ids: string[];
  comment: string;
};

export type TemplateVersion = {
  template_version_id: string;
  template_id: string;
  template_schema_id: string;
  template_schema_version: string;
  format: string;
  status: string;
};

export type DocumentSchema = {
  document_schema_id: string;
  version: string;
  status: string;
};

export type GenerationOptions = {
  knowledge_bases: string[];
  templates: TemplateVersion[];
  schemas: DocumentSchema[];
};

export type GenerationBriefView = {
  purpose: string;
  scope: Record<string, unknown>;
  source_policy: Record<string, unknown>;
  output_policy: Record<string, unknown>;
  missing_data_policy: string | null;
  inference_policy: string | null;
  confirmed: boolean;
  confidence: number;
  updated_at?: string;
};

export type ClarificationMessage = {
  message_id: string;
  role: 'assistant' | 'user' | 'system';
  content: string;
  question_id?: string | null;
  options?: string[];
  answer?: string | null;
  reason?: string | null;
  created_at?: string;
};

export type GenerationSession = {
  session_id: string;
  status: 'needs_clarification' | 'ready_to_generate' | 'generating' | 'completed' | 'cancelled';
  brief: GenerationBriefView;
  messages: ClarificationMessage[];
  work_order_id?: string | null;
};

export type WorkOrderStage =
  | 'ready'
  | 'scope_review_required'
  | 'template_contract_review_required';

export type CreateWorkOrderResult = {
  work_order_id: string;
  stage: WorkOrderStage;
  exceptions?: unknown[];
  issues?: unknown[];
};

export type WorkOrder = {
  work_order_id: string;
  status: string;
  scope_type: string;
  target_format?: string;
  [k: string]: unknown;
};

export type HarnessRunView = {
  run_id?: string;
  status?: string;
  current_node?: string;
  completed_units?: number;
  total_units?: number;
  error?: string;
} & Record<string, unknown>;

export type WorkOrderStatus = {
  work_order_id: string;
  status: string;
  phase?: string;
  display_label?: string;
  progress?: number;
  current_unit?: string;
  error_code?: string;
  error_message?: string;
  retryable?: boolean;
  next_actions?: string[];
  can_pause?: boolean;
  can_resume?: boolean;
  can_cancel?: boolean;
  can_delete?: boolean;
  generation_brief?: Record<string, unknown>;
  clarification_session_id?: string;
  scope_type: string;
  knowledge_base_name?: string;
  target_format?: string;
  unit_statuses: Record<string, string>;
  harness_run?: HarnessRunView;
  validation?: { status?: string; issues?: unknown[] };
  artifacts: Array<Record<string, unknown> & { artifact_id: string }>;
  [k: string]: unknown;
};

export type IcdScopeReview = {
  status?: string;
  exceptions?: Array<{ exception_id?: string; kind?: string; pin?: string }>;
  [k: string]: unknown;
} | null;
