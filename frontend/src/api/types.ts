/** 后端 /api/v1 的 DTO 镜像(对齐 src/api/schemas.py) */


export type Role = 'system_admin' | 'employee';

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
  owner_user_id: number | null;
  is_shared: boolean;
  is_owner: boolean;
}

export type KnowledgePurpose = 'answer' | 'compare' | 'compose';
export type KnowledgeRole = 'primary' | 'peer' | 'reference' | 'standard';

export interface KnowledgeSourceInput {
  kb_id: number;
  role: KnowledgeRole;
}

export interface KnowledgeSelectionInput {
  version: 1;
  purpose: KnowledgePurpose;
  sources: KnowledgeSourceInput[];
}

export interface AuthorizedKnowledgeSource extends KnowledgeSourceInput {
  kb_name: string;
  department_id: string;
}

export interface KnowledgeScopeSnapshot extends KnowledgeSelectionInput {
  snapshot_id: string;
  tenant_key: string;
  actor_user_id: number;
  anchor_kb_id: number;
  selection_hash: string;
  sources: AuthorizedKnowledgeSource[];
  authorized_at: string;
}

export interface FileView {
  id: string;
  name: string;
  status: string;
  processor_kind: string;
  dataset_kind: string;
  directory_id: number;
  relative_path: string;
  display_name: string;
  metadata: Record<string, unknown>;
}

export interface DirectoryView {
  id: number;
  kb_id: number;
  parent_id: number;
  name: string;
  path: string;
  depth: number;
  child_directory_count: number;
  file_count: number;
  created_by: string;
  created_at: string;
  updated_at: string;
}

export interface UploadFileResult {
  index: number;
  relative_path: string;
  status: string;
  record_id: number | null;
  error_code: string;
  message: string;
}

export interface UploadAck {
  success_count: number;
  total_count: number;
  failed_count: number;
  skipped_count: number;
  status: string;
  messages: string[];
  files: UploadFileResult[];
}

export interface DirectoryImportFileView {
  id: number;
  relative_path: string;
  source_group: string;
  status: string;
  record_id: number | null;
  error_code: string;
  error_message: string;
  attempt_count: number;
}

export interface DirectoryImportJobView {
  id: number;
  directory_id: number;
  status: string;
  total_files: number;
  success_count: number;
  failed_count: number;
  skipped_count: number;
  cancel_requested: boolean;
  created_by: string;
  last_error: string;
  created_at: string;
  updated_at: string;
  files: DirectoryImportFileView[];
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

export type DocLifecycleStatus = 'draft' | 'effective' | 'in_revision' | 'obsolete' | 'archived';
export type DocCategory =
  | 'design_doc'
  | 'schematic'
  | 'bom'
  | 'netlist'
  | 'test_report'
  | 'requirement'
  | 'standard'
  | 'other';
export type DocRelationType = 'derived_from' | 'companion' | 'references' | 'verified_by';

export interface DocAssetVersionView {
  id: number;
  asset_id: number;
  version_no: number;
  file_id: string;
  file_name: string;
  content_hash: string;
  parse_status: string;
  note: string;
  state: 'draft' | 'effective' | 'superseded';
  uploaded_by_user_id: number | null;
  uploaded_at: string;
}

export interface DocAssetEventView {
  id: number;
  asset_id: number;
  version_id: number | null;
  event: string;
  actor_user_id: number | null;
  comment: string;
  created_at: string;
}

export interface DocAssetLinkView {
  id: number;
  from_asset_id: number;
  to_asset_id: number;
  rel_type: DocRelationType;
  note: string;
  source: 'manual' | 'ai' | 'structure';
  status: 'suggested' | 'confirmed' | 'dismissed';
  created_at: string;
  to_title: string;
  to_category: string;
  to_kb_name: string;
}

export interface DocAssetView {
  id: number;
  department_id: number;
  kb_id: number;
  kb_name: string;
  project: string;
  doc_no: string;
  title: string;
  category: DocCategory;
  description: string;
  tags: string[];
  lifecycle_status: DocLifecycleStatus;
  current_version_id: number | null;
  owner_user_id: number | null;
  version_count: number;
  effective_version_no: number | null;
  created_at: string;
  updated_at: string;
}

export interface DocAssetDetailView extends DocAssetView {
  versions: DocAssetVersionView[];
  events: DocAssetEventView[];
  links_out: DocAssetLinkView[];
  links_in: DocAssetLinkView[];
}

export type WikiPageType = 'summary' | 'entity' | 'concept' | 'index';
export type WikiPageStatus = 'draft' | 'published' | 'archived';

export interface WikiPageView {
  id: number;
  kb_id: number;
  slug: string;
  title: string;
  page_type: WikiPageType;
  status: WikiPageStatus;
  summary: string;
  aliases: string[];
  source_refs: string[];
  chunk_refs: string[];
  in_links: string[];
  out_links: string[];
  version: number;
  last_edit_source: 'pipeline' | 'agent' | 'user' | 'revert';
  updated_at: string;
}

export interface WikiRevisionView {
  version: number;
  title: string;
  page_type: string;
  status: string;
  edit_source: string;
  editor_id: number | null;
  edited_at: string;
  created_at: string;
}

export interface WikiPageDetailView extends WikiPageView {
  content: string;
  backlinks: { slug: string; title: string }[];
  revisions: WikiRevisionView[];
}

export type WikiGraphViewMode = 'pages' | 'documents';

export interface WikiGraphNode {
  slug: string;
  title: string;
  page_type?: WikiPageType;
  link_count?: number;
  page_count?: number;
}

export interface WikiGraphEdge {
  source: string;
  target: string;
  weight?: number;
  pages?: string[];
}

export interface WikiGraphView {
  view?: WikiGraphViewMode;
  nodes: WikiGraphNode[];
  edges: WikiGraphEdge[];
  total: number;
  truncated: boolean;
}

export interface WikiLintIssue {
  type: string;
  severity: 'error' | 'warning' | 'info';
  slug: string;
  target: string;
  detail: string;
}

export interface WikiLintReport {
  kb_id: number;
  health_score: number;
  stats: { pages: number; entity: number; concept: number; summary: number; index: number; links: number };
  by_type: Record<string, number>;
  issues: WikiLintIssue[];
  summary: string;
}

export interface WikiStats {
  total_pages: number;
  pages_by_type: Record<string, number>;
  orphan_count: number;
}

export interface DocAssetWikiRefs {
  summary: { slug: string; title: string } | null;
  pages: { slug: string; title: string; page_type: string }[];
}

export interface WikiJobState {
  running: boolean;
  stage: string;
  error?: string;
  stats?: { documents: number; candidates: number; cited: number; pages_written: number; uncited: number; skipped_user_edited?: number; errors: string[] };
  started_at?: string;
  finished_at?: string;
}

export interface SessionView {
  id: number;
  user_id: number;
  kb_name: string;
  title: string;
  created_at: string;
  updated_at: string;
  knowledge_scope?: KnowledgeScopeSnapshot | null;
  knowledge_selection_hash?: string;
  source_access_restricted?: boolean;
  workspace_id?: string;
}

export interface CreateSessionRequest {
  kb_name: string;
  title?: string;
  knowledge_scope?: KnowledgeSelectionInput | null;
  workspace_id?: string;
}

export interface MemoryContextItem {
  id?: string;
  scope?: string;
  status?: string;
  type?: string;
  title?: string;
  content?: string;
  source_count?: number;
  has_provenance?: boolean;
  score?: number | null;
  [key: string]: unknown;
}

export interface AttachmentSnapshot {
  attachment_id: string;
  filename: string;
  media_type?: string;
  usage_hint?: string;
  content_hash?: string;
  ordinal?: number;
  parse_status?: string;
  deleted?: boolean;
}

export interface AttachmentRevisionArtifact {
  artifact_id: string;
  stage?: string;
  output_format?: string;
  download_url?: string;
}

export interface AttachmentRevisionCard {
  kind: 'attachment_revision';
  status?: string;
  content?: string;
  candidate_state?: string;
  release_state?: string;
  artifacts?: AttachmentRevisionArtifact[];
}

export interface McpWriteProposalCard {
  kind: 'mcp_write_proposal';
  proposal_id: string;
  /** Owning chat turn; absent for legacy cards. */
  turn_id?: string;
  server: string;
  server_name: string;
  tool: string;
  arguments_digest: string;
  artifact_hint: string;
  status: string;
  expires_at: string;
  artifact_ref?: string;
  artifact_filename?: string;
  artifact_sha256?: string;
  error_code?: string;
}

export interface MessageView {
  id: number;
  session_id: number;
  turn_id?: string | null;
  role: 'user' | 'assistant' | 'system';
  content: string;
  footer?: string;
  redacted?: boolean;
  edited_at?: string | null;
  created_at: string;
  memory_context?: MemoryContextItem[];
  /** Frozen attachment snapshots of the owning turn (user messages). */
  attachments?: AttachmentSnapshot[];
  /** 紧凑引用列表（随消息持久化），刷新后"参考来源"面板的数据源。 */
  citations?: EvidenceItem[];
  /** 该 turn 持久化的附件修订候选卡片（刷新后回填）。 */
  cards?: AttachmentRevisionCard[];
  /** 该 turn 持久化的执行轨迹步骤（刷新后轨迹面板回填）。 */
  trace_steps?: QueryTraceStep[];
  /** 该 turn 的外部插件写入提案卡（刷新后仍显示在对应 assistant 消息下）。 */
  mcp_write_cards?: McpWriteProposalCard[];
  /** 本轮 agent 循环预算与复杂度。 */
  budget?: TurnBudget | null;
  /** 系统校验结果（确定性检查 + 可选独立模型校验）。 */
  verification?: TurnVerification | null;
  /** 检索子 agent 摘要（不含原始上下文）。 */
  subagents?: TurnSubagent[];
}

export interface TurnBudget {
  status?: 'allocated' | 'ok' | 'partial' | 'exhausted';
  mode?: string;
  base_recursion_limit?: number;
  recursion_limit?: number;
  complexity?: {
    score?: number;
    level?: 'simple' | 'moderate' | 'complex' | string;
    signals?: string[];
    query_chars?: number;
    numbered_items?: number;
    attachments?: number;
    kb_count?: number;
  };
  exhausted?: boolean;
  stopped_reason?: string;
  tool_calls?: number;
  evidence_count?: number;
  partial_answer?: boolean;
  synthesis?: 'model' | 'notice';
  context_compressed?: {
    evidence_count?: number;
    compressed_bytes?: number;
    max_bytes?: number;
  };
  context_window?: {
    max_prompt_tokens?: number;
    window_tokens?: number;
    threshold_tokens?: number;
    note?: string;
  };
  context_compaction?: {
    stage?: string;
    trigger?: string;
    cutoff_index?: number;
    dropped_messages?: number;
    retained_messages?: number;
    before_tokens?: number;
    after_tokens?: number;
    summary_chars?: number;
    summary_sha256?: string;
    offload_path?: string;
    window_id?: string;
    window_tokens?: number;
    note?: string;
  };
}

export interface TurnVerificationCheck {
  key: string;
  label: string;
  status: 'pass' | 'warn' | 'fail' | 'unknown';
  detail?: string;
  source?: 'deterministic' | 'model_rubric' | string;
}

export interface TurnVerification {
  status: 'pass' | 'warn' | 'fail' | 'partial' | 'unknown';
  checks?: TurnVerificationCheck[];
  gaps?: string[];
  semantic_verification?: {
    status?: string;
    checks?: TurnVerificationCheck[];
    gaps?: string[];
    error?: string;
  } | null;
  budget_exhausted?: boolean;
  partial_delivery?: boolean;
  citation_coverage?: number;
  invalid_citations?: number[];
  unverified?: boolean;
}

export interface TurnSubagent {
  name?: string;
  description?: string;
  status?: 'running' | 'completed' | 'failed' | 'abandoned';
  tool_call_id?: string;
  result_chars?: number;
  duration_ms?: number;
}

export interface SessionMemorySummary {
  auto_extract_enabled: boolean;
  extracted_memories: number;
}

export interface TurnView {
  id: string;
  session_id: number;
  user_message_id: number;
  assistant_message_id: number;
  kb_name: string;
  query: string;
  query_mode: 'fast' | 'deep';
  status: 'pending' | 'streaming' | 'cancelling' | 'waiting_for_attachments' | 'completed' | 'cancelled' | 'failed';
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
  source_scope?: string;
  attachments?: AttachmentSnapshot[];
  /** P0-004 queue visibility: blocking turn, reason and wait deadline. */
  blocked_by_turn_id?: string | null;
  queue_reason?: 'waiting_for_attachments' | 'queued_behind_previous_turn' | '';
  wait_deadline?: string | null;
  knowledge_scope?: KnowledgeScopeSnapshot | null;
  knowledge_selection_hash?: string;
  source_access_restricted?: boolean;
}

/** Chat attachment lifecycle DTO (aligned with src/api/routes/attachments.py). */
export interface AttachmentView {
  attachment_id: string;
  asset_id: string;
  filename: string;
  media_type: string;
  extension: string;
  size_bytes: number;
  sha256: string;
  usage_hint: string;
  status: 'active' | 'deleted' | 'expired';
  parse_status: 'queued' | 'running' | 'ready' | 'degraded' | 'failed';
  error_code?: string;
  error_message?: string;
  manifest?: Record<string, unknown>;
  degraded_reasons?: string[];
  created_at: string;
  updated_at?: string;
}

/** Request body accepted by the persistent turn endpoint. */
export interface CreateTurnRequest {
  query: string;
  client_request_id?: string | null;
  query_mode?: 'fast' | 'deep';
  /** Session attachment references; server re-verifies ownership + parse state. */
  attachment_ids?: string[];
  /** Scope selector; "auto" expands deterministically server-side. */
  source_scope?: 'auto' | 'attachment_only' | 'knowledge_base_only' | 'attachment_and_knowledge_base';
  /** Wiki 辅助开关: true 时注册 wiki_search 检索知识 Wiki 蒸馏页。 */
  wiki_assist?: boolean;
  knowledge_scope?: KnowledgeSelectionInput | null;
}

export interface TurnStartResponse {
  turn: TurnView;
  user_message: MessageView;
}

export type MemoryStatus =
  | 'candidate'
  | 'verification_pending'
  | 'supersede_pending'
  | 'needs_rebuild'
  | 'verified'
  | 'superseded'
  | 'rejected'
  | 'deleted'
  | 'provenance_missing';

export type MemoryListStatus = MemoryStatus | 'active' | 'all';

export interface MemorySourceView {
  source_id: string;
  source_kind: string;
  session_id: number | null;
  turn_id: string | null;
  message_id: number | null;
  content_hash: string;
  valid: boolean;
}

export interface MemoryView {
  memory_id: string;
  revision: number;
  status: MemoryStatus;
  scope: 'project' | 'user';
  kind: string;
  content: Record<string, unknown>;
  source_count: number;
  sources: MemorySourceView[];
  audit: Record<string, unknown>;
  projection_status: string;
  created_at: string;
  updated_at: string;
}

export interface MemoryListResponse {
  items: MemoryView[];
  next_cursor: string | null;
  total: number | null;
}

export interface MemoryConsentView {
  consent_event_id: string;
  session_id: number;
  source_count: number;
  manifest_hash: string;
  policy_version: string;
  revoke_generation: number;
  status: 'active' | 'revoked';
  granted_at: string;
  revoked_at: string | null;
}

export interface MemoryConsentListResponse {
  items: MemoryConsentView[];
}

export interface MemoryOperationResponse {
  operation_id: string;
  memory_id: string;
  status: string;
  revision: number | null;
  message: string;
}

export interface UserMemorySettingsView {
  opt_in: boolean;
  policy_version: string;
  revoke_generation: number;
  updated_at: string;
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

export interface ExternalConversationListItem {
  conversation_id: string;
  title: string;
  source_file: string;
  origin: string;
  source_group: string;
  turn_count: number;
  block_count: number;
  status: string;
  created_at: string;
  summary?: string;
  key_points?: string[];
  summary_generated_at?: string;
}

export interface ExternalConversationsResponse {
  items: ExternalConversationListItem[];
  totals: { count: number };
}

export interface ExternalConversationTurn {
  role: string;
  content: string;
  ts?: string;
  start_offset?: number;
  end_offset?: number;
}

export interface ExternalConversationDetailResponse extends ExternalConversationListItem {
  turns: ExternalConversationTurn[];
  blocks: Record<string, unknown>[];
  preview: string;
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

/** turns SSE done 事件里的 retrieval summary(evidence 结构宽松,逐字段防御式读取) */
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

export interface ContextManifestFragment {
  kind: string;
  chars?: number;
  bytes?: number;
  count?: number;
  sha256?: string;
}

export interface ContextManifest {
  system_prompt_sha256?: string;
  system_prompt_bytes?: number;
  fragments?: ContextManifestFragment[];
  fragment_bytes?: number;
  evidence_count?: number;
  evidence_bytes?: number;
  history_turns?: number;
  model_context_window?: number;
  estimated_input_tokens?: number;
}

export interface QueryDonePayload {
  answer: string;
  summary?: {
    rewritten_queries?: string[];
    retriever_type?: string;
    final_top_k?: number;
    evidence?: EvidenceItem[];
    memory_context?: MemoryContextItem[];
    context_manifest?: ContextManifest;
    [key: string]: unknown;
  } | null;
  footer?: string;
  token_usage?: unknown;
}

export type ExportFormat = 'md' | 'xlsx' | 'docx' | 'pdf' | 'pptx';

export interface ExportArtifactView {
  artifact_id: string;
  export_job_id: string;
  session_id: number;
  format: ExportFormat;
  filename: string;
  mime_type: string;
  size: number;
  sha256: string;
  preview: Record<string, unknown>;
  created_at: string;
  expires_at?: string | null;
  preview_url: string;
  download_url: string;
  tenant_id: string;
  department_id?: string | null;
  knowledge_base_name: string;
  snapshot_id?: string;
  turn_id?: string | null;
  available?: boolean;
}

export interface ExportJobView {
  export_job_id: string;
  snapshot_id: string;
  session_id: number;
  turn_id?: string | null;
  format: ExportFormat;
  content_shape: 'report' | 'data' | 'raw' | string;
  status: 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled' | 'dead_letter' | string;
  attempt: number;
  error_message: string;
  artifact: ExportArtifactView | null;
  created_at: string;
  updated_at: string;
  completed_at: string | null;
  tenant_id: string;
  department_id?: string | null;
  knowledge_base_name: string;
}

export type ExportFormatsResponse = ExportFormat[];

export interface ExportBatchResponse {
  snapshot_id: string;
  session_id: number;
  source_ref: { kind: 'turn' | 'message' | 'snapshot'; id: string };
  jobs: ExportJobView[];
}

export type QueryTraceStatus = 'pending' | 'running' | 'done' | 'error';
export type QueryTraceKind = 'tool' | 'stage';

export interface QueryTraceStep {
  key: string;
  label: string;
  status: QueryTraceStatus;
  detail?: string;
  kind?: QueryTraceKind;
  call_id?: string;
  input?: string;
  sources?: string[];
  evidence_count?: number;
  candidate_count?: number;
  citation_numbers?: number[];
  empty_reason?: string;
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

export interface CreateUserPayload {
  username: string;
  password: string;
  role: Role;
  department_id?: number | null;
}

export interface CreateDepartmentPayload {
  name: string;
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
  employee_count: number;
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
  | 'collected'
  | 'cancel_requested'
  | 'cancelled'
  | 'completed'
  | 'failed'
  | 'invalid';
export type EvaluationRunStage = 'idle' | 'collecting' | 'collected' | 'scoring' | 'reporting';

export interface EvaluationDatasetKbBinding {
  kb_name: string;
  count: number;
}

export interface EvaluationDatasetSummary {
  name: string;
  path: string;
  sample_count: number;
  malformed_lines: number;
  kb_bindings: EvaluationDatasetKbBinding[];
  tags: string[];
  expected_denied_count: number;
  critical_count: number;
}

export interface CollectionQcReport {
  verdict: 'pass' | 'pass_with_warnings' | 'fail';
  checked_at: string;
  totals: {
    samples: number;
    snapshots: number;
    failed: number;
    degraded: number;
    zero_evidence: number;
    expected_denied: number;
  };
  issues: string[];
  warnings: string[];
  samples: Array<{
    sample_id: string;
    status: string;
    evidence_count: number;
    duration_seconds: number;
    flags: string[];
  }>;
}

export interface EvaluationKnowledgeBase {
  kb_id: number;
  kb_name: string;
  department_id: number | null;
  department_name: string | null;
  physical_exists: boolean;
  registered: boolean;
  owner_user_id: number | null;
  owner_username: string | null;
  evaluation_blocked_reason: string;
}

export interface EvaluationRunListItem {
  run_id: string;
  status: EvaluationRunStatus | '';
  has_summary: boolean;
  legacy: boolean;
  kb_id: number | null;
  kb_name: string;
  department_id: number | null;
  created_by: string;
  created_at: string;
  dataset_path: string;
  source_dataset_path: string;
  mode: string;
  report_path: string;
  dataset_sample_count: number;
  normal_sample_count: number;
  expected_denied_sample_count: number;
  cohort_fingerprint: string;
  llm_model: string;
  embedding_model: string;
  snapshot_ownership_verified: boolean;
  validation_warnings: string[];
}

export interface CreateEvaluationRunPayload {
  dataset_path: string;
  kb_id: number;
  kb_name: string;
  mode: EvaluationMode;
  sample_ids?: string[] | null;
  tags?: string[] | null;
  snapshot_path?: string | null;
}

export interface EvaluationPreflightResponse {
  dataset_path: string;
  mode: EvaluationMode;
  kb_id: number | null;
  kb_name: string;
  department_id: number | null;
  dataset_total_count: number;
  matched_sample_count: number;
  filtered_sample_count: number;
  dataset_sample_count: number;
  normal_sample_count: number;
  expected_denied_sample_count: number;
  cohort_fingerprint: string;
  warnings: string[];
  errors: string[];
  can_create: boolean;
}

export interface EvaluationGateResult {
  passed: boolean;
  verdict?: 'pass' | 'fail' | 'insufficient_evaluation';
  enforced?: boolean;
  exit_code: number;
  metric_scores: Record<string, number>;
  metric_counts: Record<string, number>;
  failures: string[];
  metric_expected_counts?: Record<string, number>;
  metric_coverage?: Record<string, number>;
  insufficient_reasons?: string[];
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
  kb_id: number | null;
  kb_name: string;
  department_id: number | null;
  created_by: string;
  source_dataset_path: string;
  dataset_sha256: string;
  execution_dataset_sha256: string;
  execution_content_sha256?: string;
  dataset_total_count: number;
  dataset_sample_count: number;
  matched_sample_count: number;
  filtered_sample_count: number;
  normal_sample_count: number;
  expected_denied_sample_count: number;
  cohort_fingerprint: string;
  snapshot_sha256: string;
  snapshot_ownership_verified: boolean;
  validation_warnings: string[];
  llm_model: string;
  embedding_model: string;
  evaluation_config: Record<string, unknown>;
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
  access_check?: {
    expected: 'allowed' | 'denied';
    observed: 'allowed' | 'denied' | 'unknown';
    reason: string;
  } | null;
}

export interface EvaluationRunDetail {
  run_id: string;
  dataset_path: string;
  snapshot_path: string;
  mode: EvaluationMode;
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
  kb_id: number | null;
  kb_name: string;
  department_id: number | null;
  created_by: string;
  created_at: string;
  source_dataset_path: string;
  dataset_sha256: string;
  execution_dataset_sha256: string;
  dataset_total_count: number;
  dataset_sample_count: number;
  matched_sample_count: number;
  filtered_sample_count: number;
  normal_sample_count: number;
  expected_denied_sample_count: number;
  cohort_fingerprint: string;
  snapshot_sha256: string;
  snapshot_ownership_verified: boolean;
  validation_warnings: string[];
  llm_model: string;
  embedding_model: string;
  evaluation_config: Record<string, unknown>;
  summary?: EvaluationSummary | null;
  sample_results: EvaluationSampleResult[];
  sample_results_error: string;
  collection_qc?: CollectionQcReport | null;
}

export interface EvaluationCompareResponse {
  strict: boolean;
  compatible: boolean;
  warnings: string[];
  compatibility: Record<string, {
    match: boolean;
    current: unknown;
    baseline: unknown;
    reason: string;
  }>;
  current: EvaluationSummary;
  baseline: EvaluationSummary;
}

export interface EvaluationDatasetUploadResponse {
  dataset_path: string;
  file_name: string;
  sample_count: number;
}

// ---------------------------------------------------------------------------
// 外部 MCP 扩展（受治理的外部工具联邦）

export type McpServerView = {
  slug: string;
  name: string;
  description: string;
  transport: string;
  url: string;
  headers: Record<string, string>;
  has_credentials: boolean;
  timeout_s: number | null;
  sse_read_timeout_s: number | null;
  enabled: boolean;
  department_scope: string;
  department_ids: number[];
  disabled_tools: string[];
  write_tools: string[];
  tags: string[];
  icon: string;
  connection_fingerprint: string;
  last_test_fingerprint: string;
  last_test_ok_at: string;
  last_test_error: string;
  credentials_error: string;
  test_passed_for_current_config: boolean;
  created_by: string;
  updated_by: string;
  created_at: string;
  updated_at: string;
};

export type McpCatalogItem = {
  slug: string;
  name: string;
  description: string;
  icon: string;
  tags: string[];
  tool_count: number;
  read_only_tool_count: number;
  subscribed: boolean;
  allow_write: boolean;
  writable: boolean;
};

export type McpListFlags = {
  enabled: boolean;
  write_enabled: boolean;
  prerequisites_met: boolean;
  prerequisites_reason: string;
};

export type McpAdminListResponse = McpListFlags & { items: McpServerView[] };
export type McpCatalogResponse = McpListFlags & { items: McpCatalogItem[] };

export type McpWriteProposal = {
  proposal_id: string;
  slug: string;
  tool_name: string;
  arguments_digest: string;
  artifact_hint: string;
  status: 'proposed' | 'confirmed' | 'rejected' | 'expired' | 'running' | 'completed' | 'failed' | string;
  artifact_ref: string;
  artifact_sha256: string;
  artifact_filename: string;
  error_code: string;
  attempts: number;
  created_at: string;
  expires_at: string;
  updated_at: string;
};

export type McpWriteProposalListResponse = { items: McpWriteProposal[] };
export type McpWriteProposalResponse = { proposal: McpWriteProposal };

export type McpToolView = {
  name: string;
  description: string;
  read_only: boolean;
  disabled: boolean;
  write_allowed: boolean;
  blocked_reason: string;
};

export type McpToolsResponse = { items: McpToolView[] };
export type McpServerResponse = { server: McpServerView };
export type McpTestResponse = {
  ok: boolean;
  tool_count: number;
  read_only_tool_count: number;
  blocked_count: number;
  latency_ms: number;
  message: string;
};

export type McpSubscriptionView = {
  slug: string;
  allow_write: boolean;
  created_at: string;
  updated_at: string;
};

export type McpSubscriptionListResponse = { items: McpSubscriptionView[] };
export type McpSubscriptionResponse = { subscription: McpSubscriptionView };

export type McpCreatePayload = {
  slug: string;
  name: string;
  transport: string;
  url: string;
  description?: string;
  headers?: Record<string, string>;
  timeout_s?: number | null;
  sse_read_timeout_s?: number | null;
  department_scope: string;
  department_ids: number[];
  tags: string[];
  icon?: string;
};
export type QualityRating = 'helpful' | 'unhelpful' | 'withdrawn';
export type QualityReason = 'factual_error' | 'unsupported_evidence' | 'not_found' | 'version_conflict' | 'incomplete_task' | 'format_mismatch';
export interface QualityFeedbackState {
  ticket_id?: string | null;
  ticket_status?: TicketStatus | null;
  support_contact?: string;
  assistant_message_id: number;
  answer_revision: string;
  case_id: string | null;
  status: string | null;
  row_version: number | null;
  rating: QualityRating | null;
  reason_codes: QualityReason[];
  comment: string;
  evidence_count: number;
  content_available: boolean;
}
export type TicketStatus = 'open' | 'in_progress' | 'waiting' | 'resolved' | 'closed';
export type TicketAction = 'reply' | 'claim' | 'assign' | 'wait' | 'resolve' | 'reopen' | 'close';
export interface FeedbackTicket {
  ticket_id: string;
  case_id: string;
  owner_user_id: number;
  requester_username: string;
  department_id: number | null;
  department_name: string;
  assignee_user_id: number | null;
  assignee_username: string;
  status: TicketStatus;
  row_version: number;
  consent_active: boolean;
  content_available: boolean;
  summary: string;
  reason_codes: QualityReason[];
  created_at: string;
  updated_at: string;
}
export interface FeedbackTicketDetail extends FeedbackTicket {
  content: { query?: string; answer?: string };
  comment: string;
  contact: string;
  resolution: string;
  events: { event_id: string; actor_user_id: number; actor_username: string; actor_role: Role; kind: string; message: string; status: TicketStatus; created_at: string }[];
}
