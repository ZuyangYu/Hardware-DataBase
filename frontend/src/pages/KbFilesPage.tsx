import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';

import { api, ApiError, isForbiddenError, uploadFilesWithProgress } from '../api/client';
import type {
  CircuitDesignDetailResponse,
  CircuitDesignRow,
  CircuitDesignsResponse,
  CircuitParseLogResponse,
  ExternalConversationDetailResponse,
  ExternalConversationListItem,
  ExternalConversationsResponse,
  FileView,
  KbView,
  OkResponse,
  ParseResultView,
  ParseTaskView,
  SchematicDesignRow,
  SchematicDesignsResponse,
  SchematicPageResponse,
  SpreadsheetLedgerResponse,
  SpreadsheetLedgerRow,
  StructuredRowsResponse,
  UploadAck,
} from '../api/types';
import type { AuthSession } from '../auth';
import AppHeader from '@/components/AppHeader';
import AppIcon from '@/components/AppIcon';
import { DataTable, type DataTableColumn } from '@/components/DataTable';
import { StatCard } from '@/components/StatCard';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Skeleton } from '@/components/ui/skeleton';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { ScrollArea } from '@/components/ui/scroll-area';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { ConfirmDialog } from '@/components/ConfirmDialog';
import { notify } from '@/components/ui/app-toast';
import { OUTLINE_ACTION_BUTTON_CLASS } from '@/lib/enterprise-ui';
import { cn } from '@/lib/utils';

// Mirrors backend canonical labels (src/pipelines/document_rag/schemas.py
// TASK_STATUS_LABELS); cross-language, so keep both in sync manually.
const STATUS_LABELS: Record<string, string> = {
  completed: '已完成',
  parsed: '已完成',
  indexed: '已索引',
  failed: '失败',
  queued: '排队中',
  pending: '待解析',
  uploading: '上传中',
  uploaded: '已上传',
  ready: '就绪',
  running: '解析中',
  parsing: '解析中',
  processing: '解析中',
  started: '解析中',
};

const PROCESSOR_LABELS: Record<string, string> = {
  document_rag: '文档',
  spreadsheet: '表格',
  spreadsheet_table: '表格',
  circuit_design: '电路',
  external_conversation: '外部对话',
};

const SOURCE_GROUPS = ['设计数据', '物料数据', '文档资料', '测试数据', '项目管理数据', '外部数据', '人员与组织数据'];
const EXTERNAL_DATA_GROUP = '外部数据';
const WORKSPACE_TABS = [
  { key: 'files', label: '文件', icon: 'file' },
  { key: 'spreadsheets', label: 'Excel 台账', icon: 'grid' },
  { key: 'circuits', label: '电路设计', icon: 'database' },
  { key: 'modules', label: '模块树', icon: 'folder' },
  { key: 'tests', label: '测试数据', icon: 'tool' },
  { key: 'schematics', label: '原理图', icon: 'eye' },
  { key: 'chats', label: '外部对话', icon: 'message' },
] as const;

type WorkspaceTab = (typeof WORKSPACE_TABS)[number]['key'];

type Props = {
  auth: AuthSession;
  kbName: string;
  onLogout: () => void;
};

export default function KbFilesPage({ auth, kbName, onLogout }: Props) {
  const navigate = useNavigate();
  const [files, setFiles] = useState<FileView[]>([]);
  const [kbMeta, setKbMeta] = useState<KbView | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [forbidden, setForbidden] = useState<string | null>(null);
  const [parseResult, setParseResult] = useState<ParseResultView | null>(null);
  const [parseLoading, setParseLoading] = useState(false);
  const [dialogOpen, setDialogOpen] = useState(false);
  const [sourceGroup, setSourceGroup] = useState('设计数据');
  const [selectedFiles, setSelectedFiles] = useState<FileList | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const [uploading, setUploading] = useState(false);
  const [uploadPercent, setUploadPercent] = useState(0);
  const [deleteTarget, setDeleteTarget] = useState<FileView | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [tasks, setTasks] = useState<ParseTaskView[]>([]);
  const [tasksLoaded, setTasksLoaded] = useState(false);
  const [activeTab, setActiveTab] = useState<WorkspaceTab>('files');
  const [spreadsheetLedger, setSpreadsheetLedger] = useState<SpreadsheetLedgerResponse | null>(null);
  const [spreadsheetsLoading, setSpreadsheetsLoading] = useState(false);
  const [selectedSpreadsheetId, setSelectedSpreadsheetId] = useState('');
  const [circuitList, setCircuitList] = useState<CircuitDesignsResponse | null>(null);
  const [circuitsLoading, setCircuitsLoading] = useState(false);
  const [selectedDesignId, setSelectedDesignId] = useState('');
  const [circuitDetail, setCircuitDetail] = useState<CircuitDesignDetailResponse | null>(null);
  const [circuitDetailLoading, setCircuitDetailLoading] = useState(false);
  const [netQuery, setNetQuery] = useState('');
  const [instanceQuery, setInstanceQuery] = useState('');
  const [parseLog, setParseLog] = useState<CircuitParseLogResponse | null>(null);
  const [parseLogLoading, setParseLogLoading] = useState(false);
  const [deleteDesignTarget, setDeleteDesignTarget] = useState<CircuitDesignRow | null>(null);
  const [deletingDesign, setDeletingDesign] = useState(false);
  const [moduleRows, setModuleRows] = useState<Record<string, unknown>[]>([]);
  const [modulesLoading, setModulesLoading] = useState(false);
  const [modulesLoaded, setModulesLoaded] = useState(false);
  const [testReports, setTestReports] = useState<Record<string, unknown>[]>([]);
  const [testMeasurements, setTestMeasurements] = useState<Record<string, unknown>[]>([]);
  const [testsLoading, setTestsLoading] = useState(false);
  const [testsLoaded, setTestsLoaded] = useState(false);
  const [testQuery, setTestQuery] = useState('');
  const [schematics, setSchematics] = useState<SchematicDesignsResponse | null>(null);
  const [schematicsLoading, setSchematicsLoading] = useState(false);
  const [selectedSchematicId, setSelectedSchematicId] = useState('');
  const [selectedPageNumber, setSelectedPageNumber] = useState<number | null>(null);
  const [schematicPage, setSchematicPage] = useState<SchematicPageResponse | null>(null);
  const [schematicPageLoading, setSchematicPageLoading] = useState(false);
  const [chats, setChats] = useState<ExternalConversationListItem[]>([]);
  const [chatsLoading, setChatsLoading] = useState(false);
  const [chatsLoaded, setChatsLoaded] = useState(false);
  const [selectedChatId, setSelectedChatId] = useState('');
  const [chatDetail, setChatDetail] = useState<ExternalConversationDetailResponse | null>(null);
  const [chatDetailLoading, setChatDetailLoading] = useState(false);
  const [deleteChatTarget, setDeleteChatTarget] = useState<ExternalConversationListItem | null>(null);
  const [deletingChat, setDeletingChat] = useState(false);
  const [summaryGenerating, setSummaryGenerating] = useState(false);

  const canWrite = kbMeta?.permission === 'write' || kbMeta?.permission === 'admin';
  const canAdmin = kbMeta?.permission === 'admin';
  const hasActiveTasks = useMemo(
    () =>
      tasks.some((task) =>
        ['queued', 'pending', 'uploading', 'uploaded', 'ready', 'running', 'parsing', 'processing', 'started'].includes(
          task.status,
        ),
      ),
    [tasks],
  );

  const loadFiles = useCallback((silent = false) => {
    let cancelled = false;
    // silent (background poll) skips flipping the loading state so the page
    // doesn't jump to skeletons every 3s and flicker.
    if (!silent) setLoaded(false);
    setForbidden(null);
    api
      .get<FileView[]>(`/api/v1/kbs/${encodeURIComponent(kbName)}/files`)
      .then((rows) => {
        if (!cancelled) setFiles(rows);
      })
      .catch((error) => {
        if (cancelled) return;
        if (isForbiddenError(error)) {
          setForbidden(error instanceof Error ? error.message : '没有该知识库的访问权限');
        } else {
          notify.error(error instanceof Error ? error.message : '加载文件列表失败');
        }
      })
      .finally(() => {
        if (!cancelled) setLoaded(true);
      });
    return () => {
      cancelled = true;
    };
  }, [kbName]);

  const loadTasks = useCallback((silent = false) => {
    if (!canWrite) {
      setTasks([]);
      setTasksLoaded(true);
      return undefined;
    }
    let cancelled = false;
    // silent (background poll) skips flipping the loading state to avoid skeleton flicker.
    if (!silent) setTasksLoaded(false);
    api
      .get<ParseTaskView[]>(`/api/v1/kbs/${encodeURIComponent(kbName)}/parse-tasks`)
      .then((rows) => {
        if (!cancelled) setTasks(rows);
      })
      .catch((error) => {
        if (!cancelled) notify.error(error instanceof Error ? error.message : '加载解析任务失败');
      })
      .finally(() => {
        if (!cancelled) setTasksLoaded(true);
      });
    return () => {
      cancelled = true;
    };
  }, [canWrite, kbName]);

  useEffect(() => {
    const cancel = loadFiles();
    return cancel;
  }, [loadFiles]);

  useEffect(() => {
    let cancelled = false;
    api
      .get<KbView[]>('/api/v1/kbs')
      .then((rows) => {
        if (!cancelled) setKbMeta(rows.find((kb) => kb.name === kbName) ?? null);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [kbName]);

  useEffect(() => {
    const cancel = loadTasks();
    return cancel;
  }, [loadTasks]);

  useEffect(() => {
    if (!canWrite || !hasActiveTasks) {
      return undefined;
    }
    const timer = window.setInterval(() => {
      loadTasks(true);
      loadFiles(true);
    }, 3000);
    return () => window.clearInterval(timer);
  }, [canWrite, hasActiveTasks, loadTasks, loadFiles]);

  const loadSpreadsheets = useCallback(async () => {
    setSpreadsheetsLoading(true);
    try {
      const result = await api.get<SpreadsheetLedgerResponse>(
        `/api/v1/kbs/${encodeURIComponent(kbName)}/structured/spreadsheets`,
      );
      setSpreadsheetLedger(result);
      setSelectedSpreadsheetId((current) => current || result.rows[0]?.file_id || '');
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '加载 Excel 台账失败');
    } finally {
      setSpreadsheetsLoading(false);
    }
  }, [kbName]);

  const loadCircuitList = useCallback(async () => {
    setCircuitsLoading(true);
    try {
      const result = await api.get<CircuitDesignsResponse>(
        `/api/v1/kbs/${encodeURIComponent(kbName)}/structured/circuit-designs`,
      );
      setCircuitList(result);
      setSelectedDesignId((current) => current || result.designs[0]?.design_id || result.failed_logs[0]?.design_id || '');
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '加载电路设计失败');
    } finally {
      setCircuitsLoading(false);
    }
  }, [kbName]);

  const loadCircuitDetail = useCallback(async () => {
    if (!selectedDesignId || circuitList?.failed_logs.some((item) => item.design_id === selectedDesignId)) {
      setCircuitDetail(null);
      return;
    }
    setCircuitDetailLoading(true);
    try {
      const params = new URLSearchParams();
      if (netQuery.trim()) params.set('net_query', netQuery.trim());
      if (instanceQuery.trim()) params.set('instance_query', instanceQuery.trim());
      const suffix = params.toString() ? `?${params.toString()}` : '';
      const result = await api.get<CircuitDesignDetailResponse>(
        `/api/v1/kbs/${encodeURIComponent(kbName)}/structured/circuit-designs/${encodeURIComponent(selectedDesignId)}${suffix}`,
      );
      setCircuitDetail(result);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '加载电路详情失败');
    } finally {
      setCircuitDetailLoading(false);
    }
  }, [circuitList?.failed_logs, instanceQuery, kbName, netQuery, selectedDesignId]);

  const loadParseLog = useCallback(async () => {
    if (!selectedDesignId) {
      setParseLog(null);
      return;
    }
    setParseLogLoading(true);
    try {
      const result = await api.get<CircuitParseLogResponse>(
        `/api/v1/kbs/${encodeURIComponent(kbName)}/structured/circuit-designs/${encodeURIComponent(selectedDesignId)}/parse-log`,
      );
      setParseLog(result);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '加载解析日志失败');
    } finally {
      setParseLogLoading(false);
    }
  }, [kbName, selectedDesignId]);

  const loadModules = useCallback(async () => {
    setModulesLoading(true);
    try {
      const result = await api.get<StructuredRowsResponse>(
        `/api/v1/kbs/${encodeURIComponent(kbName)}/structured/modules`,
      );
      setModuleRows(result.rows);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '加载模块树失败');
    } finally {
      setModulesLoaded(true);
      setModulesLoading(false);
    }
  }, [kbName]);

  const loadTests = useCallback(async () => {
    setTestsLoading(true);
    try {
      const params = new URLSearchParams();
      if (testQuery.trim()) params.set('query', testQuery.trim());
      params.set('limit', '100');
      const [reports, measurements] = await Promise.all([
        api.get<StructuredRowsResponse>(`/api/v1/kbs/${encodeURIComponent(kbName)}/structured/test-reports`),
        api.get<StructuredRowsResponse>(
          `/api/v1/kbs/${encodeURIComponent(kbName)}/structured/test-measurements?${params.toString()}`,
        ),
      ]);
      setTestReports(reports.rows);
      setTestMeasurements(measurements.rows);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '加载测试数据失败');
    } finally {
      setTestsLoaded(true);
      setTestsLoading(false);
    }
  }, [kbName, testQuery]);

  const loadSchematics = useCallback(async () => {
    setSchematicsLoading(true);
    try {
      const result = await api.get<SchematicDesignsResponse>(
        `/api/v1/kbs/${encodeURIComponent(kbName)}/structured/schematics`,
      );
      setSchematics(result);
      const first = result.designs[0];
      setSelectedSchematicId((current) => current || first?.design_id || '');
      setSelectedPageNumber((current) => current ?? first?.pages[0]?.page_number ?? null);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '加载原理图失败');
    } finally {
      setSchematicsLoading(false);
    }
  }, [kbName]);

  const loadSchematicPage = useCallback(async () => {
    if (!selectedSchematicId || selectedPageNumber == null) {
      setSchematicPage(null);
      return;
    }
    setSchematicPageLoading(true);
    try {
      const result = await api.get<SchematicPageResponse>(
        `/api/v1/kbs/${encodeURIComponent(kbName)}/structured/schematics/${encodeURIComponent(selectedSchematicId)}/pages/${selectedPageNumber}`,
      );
      setSchematicPage(result);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '加载原理图页面失败');
    } finally {
      setSchematicPageLoading(false);
    }
  }, [kbName, selectedPageNumber, selectedSchematicId]);

  const loadChats = useCallback(async () => {
    setChatsLoading(true);
    try {
      const result = await api.get<ExternalConversationsResponse>(
        `/api/v1/kbs/${encodeURIComponent(kbName)}/external-conversations`,
      );
      setChats(result.items);
      setSelectedChatId((current) =>
        result.items.some((item) => item.conversation_id === current) ? current : result.items[0]?.conversation_id ?? '',
      );
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '加载外部对话失败');
    } finally {
      setChatsLoaded(true);
      setChatsLoading(false);
    }
  }, [kbName]);

  const loadChatDetail = useCallback(async () => {
    if (!selectedChatId) {
      setChatDetail(null);
      return;
    }
    setChatDetailLoading(true);
    try {
      const result = await api.get<ExternalConversationDetailResponse>(
        `/api/v1/kbs/${encodeURIComponent(kbName)}/external-conversations/${encodeURIComponent(selectedChatId)}`,
      );
      setChatDetail(result);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '加载对话内容失败');
    } finally {
      setChatDetailLoading(false);
    }
  }, [kbName, selectedChatId]);

  useEffect(() => {
    if (activeTab === 'spreadsheets' && !spreadsheetLedger && !spreadsheetsLoading) void loadSpreadsheets();
    if (activeTab === 'circuits' && !circuitList && !circuitsLoading) void loadCircuitList();
    if (activeTab === 'modules' && !modulesLoaded && !modulesLoading) void loadModules();
    if (activeTab === 'tests' && !testsLoaded && !testsLoading) void loadTests();
    if (activeTab === 'schematics' && !schematics && !schematicsLoading) void loadSchematics();
    if (activeTab === 'chats' && !chatsLoaded && !chatsLoading) void loadChats();
  }, [
    activeTab,
    chatDetail,
    chatsLoaded,
    chatsLoading,
    circuitList,
    circuitsLoading,
    loadCircuitList,
    loadModules,
    loadSchematics,
    loadSpreadsheets,
    loadTests,
    modulesLoaded,
    modulesLoading,
    schematics,
    schematicsLoading,
    spreadsheetLedger,
    spreadsheetsLoading,
    testsLoaded,
    testsLoading,
  ]);

  useEffect(() => {
    if (activeTab === 'circuits') {
      void loadCircuitDetail();
      void loadParseLog();
    }
  }, [activeTab, selectedDesignId]);

  useEffect(() => {
    if (activeTab === 'schematics') void loadSchematicPage();
  }, [activeTab, loadSchematicPage]);

  useEffect(() => {
    if (activeTab === 'chats') void loadChatDetail();
  }, [activeTab, loadChatDetail]);

  async function handleViewChunks(file: FileView) {
    setParseLoading(true);
    setDialogOpen(true);
    setParseResult(null);
    try {
      const result = await api.get<ParseResultView>(
        `/api/v1/kbs/${encodeURIComponent(kbName)}/files/${encodeURIComponent(file.id)}/chunks`,
      );
      setParseResult(result);
    } catch (error) {
      setDialogOpen(false);
      notify.error(error instanceof Error ? error.message : '加载解析结果失败');
    } finally {
      setParseLoading(false);
    }
  }

  async function handleUpload() {
    if (!selectedFiles || selectedFiles.length === 0) {
      notify.error('请选择文件');
      return;
    }
    // M15: 提交前逐文件预检(扩展名/总大小)，避免整批到服务器才被 413 拒收。
    const supportedExts =
      sourceGroup === EXTERNAL_DATA_GROUP
        ? ['.txt', '.md', '.markdown']
        : ['.pdf', '.doc', '.docx', '.xlsx', '.edf', '.edif'];
    const maxTotalBytes = 512 * 1024 * 1024; // 与后端 upload.py MAX_UPLOAD_BYTES 默认一致
    let totalBytes = 0;
    const rejected: string[] = [];
    for (const file of Array.from(selectedFiles)) {
      totalBytes += file.size;
      const ext = '.' + (file.name.split('.').pop() || '').toLowerCase();
      if (!supportedExts.includes(ext)) rejected.push(`${file.name}（不支持 ${ext || '无扩展名'}）`);
    }
    if (rejected.length > 0) {
      notify.error(`以下文件类型不被支持，已取消上传：${rejected.join('、')}`);
      return;
    }
    if (totalBytes > maxTotalBytes) {
      notify.error(`批次总大小 ${(totalBytes / 1024 / 1024).toFixed(1)}MB 超过 ${maxTotalBytes / 1024 / 1024}MB 上限，请分批上传`);
      return;
    }
    setUploading(true);
    setUploadPercent(0);
    try {
      const form = new FormData();
      Array.from(selectedFiles).forEach((file) => form.append('files', file));
      form.append('source_group', sourceGroup);
      const result = await uploadFilesWithProgress<UploadAck>(
        `/api/v1/kbs/${encodeURIComponent(kbName)}/files`,
        form,
        (percent) => setUploadPercent(percent),
      );
      const message = result.messages[0] || `成功 ${result.success_count}/${result.total_count}`;
      if (result.failed_count > 0 || result.skipped_count > 0) {
        notify.warning(message);
      } else {
        notify.success(message);
      }
      setSelectedFiles(null);
      if (fileInputRef.current) fileInputRef.current.value = '';
      await Promise.all([loadFiles(), loadTasks()]);
    } catch (error) {
      if (error instanceof ApiError && error.status === 413) {
        notify.error(`批次超过服务器上传大小上限，已被拒收；请减小批量后重试（${error.message.slice(0, 80)}）`);
      } else {
        notify.error(error instanceof Error ? error.message : '上传失败');
      }
    } finally {
      setUploading(false);
      setUploadPercent(0);
    }
  }

  async function handleDeleteFile() {
    if (!deleteTarget) return;
    setDeleting(true);
    try {
      await api.delete<OkResponse>(
        `/api/v1/kbs/${encodeURIComponent(kbName)}/files/${encodeURIComponent(deleteTarget.id)}`,
      );
      notify.success('文件已删除');
      setDeleteTarget(null);
      await Promise.all([loadFiles(), loadTasks()]);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '删除失败');
    } finally {
      setDeleting(false);
    }
  }

  async function handleDeleteDesign() {
    if (!deleteDesignTarget) return;
    setDeletingDesign(true);
    try {
      await api.delete<OkResponse>(
        `/api/v1/kbs/${encodeURIComponent(kbName)}/structured/circuit-designs/${encodeURIComponent(deleteDesignTarget.design_id)}`,
      );
      notify.success('电路设计已删除');
      setDeleteDesignTarget(null);
      setSelectedDesignId('');
      setCircuitDetail(null);
      setParseLog(null);
      await Promise.all([loadCircuitList(), loadModules(), loadSchematics(), loadFiles()]);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '删除电路设计失败');
    } finally {
      setDeletingDesign(false);
    }
  }

  async function handleRegenerateSummary() {
    if (!selectedChatId || summaryGenerating) return;
    setSummaryGenerating(true);
    try {
      const result = await api.post<ExternalConversationDetailResponse>(
        `/api/v1/kbs/${encodeURIComponent(kbName)}/external-conversations/${encodeURIComponent(selectedChatId)}/summary`,
        {},
      );
      setChatDetail(result);
      notify.success('AI 摘要已生成');
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '摘要生成失败');
    } finally {
      setSummaryGenerating(false);
    }
  }

  async function handleDeleteChat() {    if (!deleteChatTarget) return;
    setDeletingChat(true);
    try {
      await api.delete<OkResponse>(
        `/api/v1/kbs/${encodeURIComponent(kbName)}/external-conversations/${encodeURIComponent(deleteChatTarget.conversation_id)}`,
      );
      notify.success('外部对话已删除');
      setDeleteChatTarget(null);
      setSelectedChatId('');
      setChatDetail(null);
      await loadChats();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '删除外部对话失败');
    } finally {
      setDeletingChat(false);
    }
  }

  async function deleteTask(task: ParseTaskView) {
    try {
      await api.delete<OkResponse>(
        `/api/v1/kbs/${encodeURIComponent(kbName)}/parse-tasks/${encodeURIComponent(task.id)}`,
      );
      notify.success('解析任务已删除');
      loadTasks();
      loadFiles();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '任务操作失败');
    }
  }

  async function clearFinishedTasks() {
    try {
      await api.delete<OkResponse>(`/api/v1/kbs/${encodeURIComponent(kbName)}/parse-tasks/finished`);
      notify.success('已清理完成/失败任务');
      loadTasks();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '清理失败');
    }
  }

  if (forbidden) {
    return (
      <div className="flex min-h-full items-center justify-center px-[48px] py-[32px]">
        <div className="flex max-w-[420px] flex-col items-center gap-[12px] rounded-[16px] border border-[#e3e7f1] bg-white p-[36px] text-center shadow-[0_8px_24px_rgba(17,17,17,0.045)]">
          <AppIcon name="warning" size={36} className="text-[#b45309]" />
          <div className="text-[15px] font-semibold text-[#18181a]">无法访问该知识库</div>
          <div className="text-[13px] leading-[20px] text-[#757f9c]">{forbidden}</div>
        </div>
      </div>
    );
  }

  const columns: DataTableColumn<FileView>[] = useMemo(() => {
    const base: DataTableColumn<FileView>[] = [
      {
        key: 'name',
        title: '文件名',
        render: (file) => <span className="truncate font-medium text-[#18181a]">{file.name}</span>,
      },
      {
        key: 'type',
        title: '类型',
        width: 100,
        render: (file) => (
          <span className="inline-flex rounded-full bg-[#f3f4f6] px-[8px] py-[2px] text-[11px] text-[#464c5e]">
            {PROCESSOR_LABELS[file.processor_kind] ?? file.processor_kind ?? '未知'}
          </span>
        ),
      },
      {
        key: 'status',
        title: '状态',
        width: 100,
        render: (file) =>
          file.status === 'failed' ? (
            <span className="text-[#d20b0b]">{STATUS_LABELS[file.status] ?? file.status ?? '未知'}</span>
          ) : (
            <span className="text-[#464c5e]">{STATUS_LABELS[file.status] ?? file.status ?? '未知'}</span>
          ),
      },
      {
        key: 'dataset',
        title: '数据集',
        width: 120,
        render: (file) => <span className="text-[#858b9c]">{file.dataset_kind || '-'}</span>,
      },
    ];
    if (!canAdmin) return base;
    return [
      ...base,
      {
        key: 'actions',
        title: '操作',
        width: 90,
        align: 'right',
        render: (file) => (
            <button
              type="button"
              onClick={(event) => {
                event.stopPropagation();
                setDeleteTarget(file);
              }}
              className="inline-flex h-[28px] items-center gap-[4px] rounded-[8px] border border-[#e3e7f1] bg-white px-[12px] text-[12px] text-[#d20b0b] transition-colors hover:border-[#f3b0b0] hover:bg-[#fce7e7]"
            >
              删除
            </button>
          ),
      },
    ];
  }, [canAdmin]);

  return (
    <div className="min-h-full px-[48px] pt-[32px] pb-[43px] max-[900px]:px-[16px]">
      <AppHeader
        title={`知识库工作台 · ${kbName}`}
        description={`共 ${files.length} 个文件;上传、解析任务和结构化结果在当前知识库内处理。`}
        userName={auth.user.username}
        onLogout={onLogout}
      />

      <div className="mt-[16px] flex flex-wrap justify-end gap-[8px]">
        <Button
          variant="outline"
          className={cn(OUTLINE_ACTION_BUTTON_CLASS, 'h-[34px] px-[12px]')}
          onClick={() => navigate('/kbs')}
        >
          <AppIcon name="database" size={14} />
          全部知识库
        </Button>
        <Button
          className="h-[34px] gap-[6px] rounded-[10px] bg-[#18181a] px-[14px] text-[13px] text-white hover:bg-[#303030]"
          onClick={() => navigate(`/chat?kb=${encodeURIComponent(kbName)}`)}
        >
          <AppIcon name="send" size={14} />
          打开对话
        </Button>
      </div>

      {canWrite && (
        <div className="mt-[16px] flex flex-wrap items-end gap-[10px] rounded-[14px] bg-white p-[16px] shadow-[0_8px_24px_rgba(17,17,17,0.045)]">
          <div className="grid min-w-[180px] gap-[4px]">
            <span className="text-[11px] text-[#858b9c]">文件类型</span>
            <Select value={sourceGroup} onValueChange={setSourceGroup}>
              <SelectTrigger className="h-[34px] w-full rounded-[10px] border-[#e3e7f1] bg-white text-[13px]">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {SOURCE_GROUPS.map((group) => (
                  <SelectItem key={group} value={group}>{group}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <label className="grid min-w-[280px] flex-1 gap-[4px]">
            <span className="text-[11px] text-[#858b9c]">上传文件</span>
            <input
              ref={fileInputRef}
              type="file"
              multiple
              accept={
                sourceGroup === EXTERNAL_DATA_GROUP
                  ? '.txt,.md,.markdown'
                  : '.pdf,.doc,.docx,.xlsx,.edf,.edif'
              }
              onChange={(event) => setSelectedFiles(event.target.files)}
              className="h-[34px] rounded-[10px] border border-[#e3e7f1] bg-white px-[10px] py-[5px] text-[12px] text-[#464c5e] file:mr-[10px] file:rounded-[8px] file:border-0 file:bg-[#f3f4f6] file:px-[10px] file:py-[4px] file:text-[12px] file:text-[#464c5e]"
            />
          </label>
          <Button
            onClick={handleUpload}
            disabled={uploading}
            className="h-[34px] gap-[6px] rounded-[10px] bg-[#18181a] px-[16px] text-[13px] text-white hover:bg-[#303030]"
          >
            <AppIcon name="plus" size={14} />
            {uploading ? `上传中 ${uploadPercent}%` : '开始上传'}
          </Button>
        </div>
      )}

      {canWrite && (
        <div className="mt-[16px] rounded-[14px] bg-white p-[16px] shadow-[0_8px_24px_rgba(17,17,17,0.045)]">
          <div className="mb-[12px] flex flex-wrap items-center justify-between gap-[10px]">
            <h3 className="text-[14px] font-semibold text-[#18181a]">解析任务</h3>
            <div className="flex gap-[8px]">
              <Button variant="outline" className={cn(OUTLINE_ACTION_BUTTON_CLASS, 'h-[30px] px-[10px]')} onClick={() => loadTasks()}>
                <AppIcon name="refresh" size={13} />
                刷新
              </Button>
              <Button variant="outline" className={cn(OUTLINE_ACTION_BUTTON_CLASS, 'h-[30px] px-[10px]')} onClick={clearFinishedTasks}>
                清理完成
              </Button>
            </div>
          </div>
          {!tasksLoaded ? (
            <div className="grid gap-[8px]">
              {[0, 1].map((i) => <Skeleton key={i} className="h-[44px] rounded-[10px]" />)}
            </div>
          ) : tasks.length === 0 ? (
            <div className="py-[18px] text-center text-[12px] text-[#858b9c]">暂无解析任务</div>
          ) : (
            <div className="grid gap-[8px]">
              {tasks.map((task) => (
                <ParseTaskRow key={task.id} task={task} onDelete={deleteTask} />
              ))}
            </div>
          )}
        </div>
      )}

      <div className="mt-[20px] overflow-hidden rounded-[20px_20px_0_0] bg-white shadow-[0_-4px_16px_0_rgba(0,0,0,0.05)]">
        <div className="flex gap-[4px] overflow-x-auto border-b border-[#f0f1f4] px-[18px] pt-[14px]">
          {WORKSPACE_TABS.map((tab) => (
            <button
              key={tab.key}
              type="button"
              onClick={() => setActiveTab(tab.key)}
              className={cn(
                'inline-flex h-[38px] shrink-0 items-center gap-[6px] border-b-2 px-[12px] text-[13px] transition-colors',
                activeTab === tab.key
                  ? 'border-[#18181a] text-[#18181a]'
                  : 'border-transparent text-[#757f9c] hover:text-[#18181a]',
              )}
            >
              <AppIcon name={tab.icon} size={14} />
              {tab.label}
            </button>
          ))}
        </div>
        <div className="p-[18px_18px_24px]">
          {activeTab === 'files' && (
            <FilesPanel
              loaded={loaded}
              files={files}
              columns={columns}
              onViewChunks={(file) => void handleViewChunks(file)}
            />
          )}
          {activeTab === 'spreadsheets' && (
            <SpreadsheetPanel
              ledger={spreadsheetLedger}
              loading={spreadsheetsLoading}
              selectedId={selectedSpreadsheetId}
              onSelectedIdChange={setSelectedSpreadsheetId}
              onRefresh={() => void loadSpreadsheets()}
            />
          )}
          {activeTab === 'circuits' && (
            <CircuitPanel
              circuitList={circuitList}
              loading={circuitsLoading}
              selectedDesignId={selectedDesignId}
              onSelectedDesignIdChange={setSelectedDesignId}
              detail={circuitDetail}
              detailLoading={circuitDetailLoading}
              netQuery={netQuery}
              onNetQueryChange={setNetQuery}
              instanceQuery={instanceQuery}
              onInstanceQueryChange={setInstanceQuery}
              parseLog={parseLog}
              parseLogLoading={parseLogLoading}
              canWrite={canWrite}
              onRefresh={() => {
                void loadCircuitList();
                void loadCircuitDetail();
                void loadParseLog();
              }}
              onDeleteDesign={setDeleteDesignTarget}
            />
          )}
          {activeTab === 'modules' && (
            <ModulePanel rows={moduleRows} loading={modulesLoading} onRefresh={() => void loadModules()} />
          )}
          {activeTab === 'tests' && (
            <TestDataPanel
              reports={testReports}
              measurements={testMeasurements}
              loading={testsLoading}
              query={testQuery}
              onQueryChange={setTestQuery}
              onRefresh={() => void loadTests()}
            />
          )}
          {activeTab === 'schematics' && (
            <SchematicPanel
              data={schematics}
              loading={schematicsLoading}
              selectedDesignId={selectedSchematicId}
              onSelectedDesignIdChange={(value) => {
                setSelectedSchematicId(value);
                const next = schematics?.designs.find((design) => design.design_id === value);
                setSelectedPageNumber(next?.pages[0]?.page_number ?? null);
              }}
              selectedPageNumber={selectedPageNumber}
              onSelectedPageNumberChange={setSelectedPageNumber}
              page={schematicPage}
              pageLoading={schematicPageLoading}
              onRefresh={() => {
                void loadSchematics();
                void loadSchematicPage();
              }}
            />
          )}
          {activeTab === 'chats' && (
            <ChatsPanel
              items={chats}
              loading={chatsLoading || chatDetailLoading}
              selectedId={selectedChatId}
              detail={chatDetail}
              canWrite={canWrite}
              summaryGenerating={summaryGenerating}
              onSelectedIdChange={setSelectedChatId}
              onRefresh={() => void loadChats()}
              onDelete={setDeleteChatTarget}
              onRegenerateSummary={() => void handleRegenerateSummary()}
            />
          )}
        </div>
      </div>

      <Dialog open={dialogOpen} onOpenChange={setDialogOpen}>
        <DialogContent className="flex max-h-[calc(100dvh-64px)] w-[calc(100%-32px)] max-w-[760px] flex-col overflow-hidden rounded-[16px] p-0">
          <DialogHeader className="px-[24px] pt-[20px]">
            <DialogTitle className="truncate text-[16px] font-semibold text-[#18181a]">
              解析分块{parseResult ? ` · ${parseResult.file_name}(${parseResult.chunk_count} 块)` : ''}
            </DialogTitle>
          </DialogHeader>
          <ScrollArea className="min-h-0 flex-1">
            <div className="grid gap-[10px] px-[24px] pb-[20px]">
              {parseLoading &&
                [0, 1, 2].map((i) => <Skeleton key={i} className="h-[72px] rounded-[10px]" />)}
              {!parseLoading && parseResult && parseResult.chunks.length === 0 && (
                <div className="py-[24px] text-center text-[13px] text-[#858b9c]">
                  该文件暂无解析分块(可能仍在解析中)
                </div>
              )}
              {!parseLoading &&
                parseResult?.chunks.map((chunk) => (
                  <div
                    key={chunk.index}
                    className="rounded-[10px] border border-[#e3e7f1] bg-[#fafbfc] px-[12px] py-[10px]"
                  >
                    <div className="mb-[4px] text-[11px] font-semibold text-[#757f9c]">
                      #{chunk.index + 1}
                    </div>
                    <div className="whitespace-pre-wrap break-words text-[12px] leading-[18px] text-[#464c5e]">
                      {chunk.content}
                    </div>
                  </div>
                ))}
            </div>
          </ScrollArea>
        </DialogContent>
      </Dialog>

      <ConfirmDialog
        open={deleteTarget !== null}
        onOpenChange={(open: boolean) => {
          if (!open) setDeleteTarget(null);
        }}
        title={<>删除文件「{deleteTarget?.name}」</>}
        description="删除后该文件的远端文档、归档和解析结果将不可恢复。"
        confirmText="删除"
        loading={deleting}
        destructive
        onConfirm={handleDeleteFile}
      />
      <ConfirmDialog
        open={deleteDesignTarget !== null}
        onOpenChange={(open: boolean) => {
          if (!open) setDeleteDesignTarget(null);
        }}
        title={<>删除电路设计「{deleteDesignTarget?.design_id}」</>}
        description="删除后该设计的结构化状态、索引、解析日志和归档文件会被清理。"
        confirmText="删除"
        loading={deletingDesign}
        destructive
        onConfirm={handleDeleteDesign}
      />
      <ConfirmDialog
        open={deleteChatTarget !== null}
        onOpenChange={(open: boolean) => {
          if (!open) setDeleteChatTarget(null);
        }}
        title={<>删除外部对话「{deleteChatTarget?.title}」</>}
        description="删除后该对话的解析结果、索引和归档文件会被清理。"
        confirmText="删除"
        loading={deletingChat}
        destructive
        onConfirm={handleDeleteChat}
      />
    </div>
  );
}

function FilesPanel({
  loaded,
  files,
  columns,
  onViewChunks,
}: {
  loaded: boolean;
  files: FileView[];
  columns: DataTableColumn<FileView>[];
  onViewChunks: (file: FileView) => void;
}) {
  if (!loaded) return <PanelSkeleton rows={3} />;
  if (files.length === 0) return <EmptyState text="该知识库暂无文件" />;
  return (
    <DataTable
      columns={columns}
      data={files}
      rowKey={(file) => file.id}
      size="compact"
      onRowClick={onViewChunks}
      emptyText="暂无文件"
    />
  );
}

function SpreadsheetPanel({
  ledger,
  loading,
  selectedId,
  onSelectedIdChange,
  onRefresh,
}: {
  ledger: SpreadsheetLedgerResponse | null;
  loading: boolean;
  selectedId: string;
  onSelectedIdChange: (value: string) => void;
  onRefresh: () => void;
}) {
  const rows = ledger?.rows ?? [];
  const selected = rows.find((row) => row.file_id === selectedId) ?? rows[0];
  const sheetRows = selected?.sheets ?? [];
  const columns: DataTableColumn<SpreadsheetLedgerRow>[] = [
    { key: 'file_name', title: '文件', render: (row) => <TextCell value={row.file_name} strong /> },
    { key: 'status', title: '状态', width: 90, render: (row) => <StatusText status={row.status} label={row.status_label} /> },
    { key: 'sheets', title: '工作表', width: 80, align: 'right', render: (row) => row.sheet_count },
    { key: 'rows', title: '有效行', width: 90, align: 'right', render: (row) => row.row_count },
    { key: 'semantic', title: '语义行', width: 90, align: 'right', render: (row) => row.semantic_row_count },
    { key: 'blocks', title: '文本块', width: 90, align: 'right', render: (row) => row.block_count },
    { key: 'objects', title: '对象', width: 80, align: 'right', render: (row) => row.object_count },
  ];
  const sheetColumns: DataTableColumn<Record<string, unknown>>[] = [
    { key: 'sheet', title: '工作表', render: (row) => <TextCell value={valueOf(row, 'sheet_name')} strong /> },
    { key: 'row_count', title: '行数', width: 80, align: 'right', render: (row) => valueOf(row, 'row_count') },
    { key: 'column_count', title: '列数', width: 80, align: 'right', render: (row) => valueOf(row, 'column_count') },
    { key: 'non_empty', title: '有效行', width: 80, align: 'right', render: (row) => valueOf(row, 'non_empty_row_count') },
    { key: 'cells', title: '单元格', width: 90, align: 'right', render: (row) => valueOf(row, 'non_empty_cell_count') },
    { key: 'header', title: '表头行', width: 80, align: 'right', render: (row) => valueOf(row, 'header_row_index') || '-' },
    { key: 'semantic', title: '语义行', width: 90, align: 'right', render: (row) => valueOf(row, 'semantic_row_count') },
  ];
  return (
    <div className="grid gap-[16px]">
      <PanelToolbar title="Excel 结构化台账" onRefresh={onRefresh} loading={loading} />
      {loading && !ledger ? (
        <PanelSkeleton rows={3} />
      ) : rows.length === 0 ? (
        <EmptyState text="当前知识库没有结构化 Excel 文件" />
      ) : (
        <>
          <div className="grid grid-cols-4 gap-[10px] max-[900px]:grid-cols-2">
            <StatCard value={ledger?.totals.file_count ?? 0} label="Excel 文件" />
            <StatCard value={ledger?.totals.sheet_count ?? 0} label="工作表" />
            <StatCard value={ledger?.totals.semantic_row_count ?? 0} label="语义行" />
            <StatCard value={ledger?.totals.pending_count ?? 0} label="待处理" tone={(ledger?.totals.pending_count ?? 0) > 0 ? 'red' : 'green'} />
          </div>
          <DataTable columns={columns} data={rows} rowKey={(row) => row.file_id} size="compact" />
          <div className="grid gap-[10px]">
            <div className="flex flex-wrap items-center justify-between gap-[10px]">
              <h3 className="text-[14px] font-semibold text-[#18181a]">工作表明细</h3>
              <Select value={selected?.file_id ?? ''} onValueChange={onSelectedIdChange}>
                <SelectTrigger className="h-[32px] w-[260px] max-w-full rounded-[10px] border-[#e3e7f1] text-[12px]">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {rows.map((row) => (
                    <SelectItem key={row.file_id} value={row.file_id}>{row.file_name}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <DataTable columns={sheetColumns} data={sheetRows} rowKey={(row, index) => `${valueOf(row, 'sheet_name')}-${index}`} size="compact" emptyText="暂无工作表明细" />
          </div>
        </>
      )}
    </div>
  );
}

function CircuitPanel({
  circuitList,
  loading,
  selectedDesignId,
  onSelectedDesignIdChange,
  detail,
  detailLoading,
  netQuery,
  onNetQueryChange,
  instanceQuery,
  onInstanceQueryChange,
  parseLog,
  parseLogLoading,
  canWrite,
  onRefresh,
  onDeleteDesign,
}: {
  circuitList: CircuitDesignsResponse | null;
  loading: boolean;
  selectedDesignId: string;
  onSelectedDesignIdChange: (value: string) => void;
  detail: CircuitDesignDetailResponse | null;
  detailLoading: boolean;
  netQuery: string;
  onNetQueryChange: (value: string) => void;
  instanceQuery: string;
  onInstanceQueryChange: (value: string) => void;
  parseLog: CircuitParseLogResponse | null;
  parseLogLoading: boolean;
  canWrite: boolean;
  onRefresh: () => void;
  onDeleteDesign: (row: CircuitDesignRow) => void;
}) {
  const designs = circuitList?.designs ?? [];
  const failedLogs = circuitList?.failed_logs ?? [];
  const selectedDesign = designs.find((row) => row.design_id === selectedDesignId);
  const isFailedOnly = failedLogs.some((row) => row.design_id === selectedDesignId) && !selectedDesign;
  const moduleColumns: DataTableColumn<Record<string, unknown>>[] = [
    { key: 'name', title: '模块', render: (row) => <TextCell value={valueOf(row, 'name')} strong /> },
    { key: 'module_id', title: 'ID', render: (row) => <TextCell value={valueOf(row, 'module_id')} /> },
    { key: 'instances', title: '实例', width: 80, align: 'right', render: (row) => valueOf(row, 'instance_count') },
    { key: 'nets', title: '网络', width: 80, align: 'right', render: (row) => valueOf(row, 'net_count') },
  ];
  const netColumns: DataTableColumn<Record<string, unknown>>[] = [
    { key: 'name', title: '网络', render: (row) => <TextCell value={valueOf(row, 'name')} strong /> },
    { key: 'type', title: '类型', width: 90, render: (row) => valueOf(row, 'net_type') },
    { key: 'connections', title: '连接数', width: 90, align: 'right', render: (row) => valueOf(row, 'connection_count') },
    { key: 'design', title: '设计', width: 120, render: (row) => valueOf(row, 'design_id') },
  ];
  const instanceColumns: DataTableColumn<Record<string, unknown>>[] = [
    { key: 'refdes', title: '实例', width: 100, render: (row) => <TextCell value={valueOf(row, 'refdes')} strong /> },
    { key: 'cell', title: '器件', render: (row) => <TextCell value={valueOf(row, 'library_cell')} /> },
    { key: 'part', title: '料号', render: (row) => <TextCell value={valueOf(row, 'part_number')} /> },
    { key: 'value', title: '值', width: 120, render: (row) => valueOf(row, 'value') },
  ];
  const refColumns: DataTableColumn<Record<string, unknown>>[] = [
    { key: 'edf', title: 'EDF', width: 120, render: (row) => <TextCell value={valueOf(row, 'edf_refdes')} strong /> },
    { key: 'pdf', title: 'PDF 标签', render: (row) => <TextCell value={valueOf(row, 'pdf_label')} /> },
    { key: 'page', title: '页码', width: 80, align: 'right', render: (row) => valueOf(row, 'page_number') },
    { key: 'confidence', title: '置信度', width: 90, align: 'right', render: (row) => formatPercent(valueOf(row, 'confidence')) },
  ];
  return (
    <div className="grid gap-[16px]">
      <PanelToolbar title="电路设计浏览" onRefresh={onRefresh} loading={loading || detailLoading || parseLogLoading} />
      {loading && !circuitList ? (
        <PanelSkeleton rows={3} />
      ) : designs.length === 0 && failedLogs.length === 0 ? (
        <EmptyState text="当前知识库尚未解析 EDF 网表或 PDF 原理图" />
      ) : (
        <>
          <div className="flex flex-wrap items-end gap-[10px]">
            <div className="grid min-w-[260px] gap-[4px]">
              <span className="text-[11px] text-[#858b9c]">设计</span>
              <Select value={selectedDesignId} onValueChange={onSelectedDesignIdChange}>
                <SelectTrigger className="h-[34px] w-full rounded-[10px] border-[#e3e7f1] text-[12px]">
                  <SelectValue placeholder="选择设计" />
                </SelectTrigger>
                <SelectContent>
                  {designs.map((row) => (
                    <SelectItem key={row.design_id} value={row.design_id}>
                      {row.design_id} · {row.module_count} 模块
                    </SelectItem>
                  ))}
                  {failedLogs.map((row) => (
                    <SelectItem key={`failed-${row.design_id}`} value={row.design_id}>
                      {row.design_id} · 解析失败
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            {canWrite && selectedDesign && (
              <Button variant="outline" className={cn(OUTLINE_ACTION_BUTTON_CLASS, 'h-[34px] px-[12px] text-[#d20b0b]')} onClick={() => onDeleteDesign(selectedDesign)}>
                <AppIcon name="trash" size={14} />
                删除
              </Button>
            )}
          </div>
          {selectedDesign && detail?.summary && (
            <div className="grid grid-cols-4 gap-[10px] max-[900px]:grid-cols-2">
              <StatCard value={String(detail.summary.instances ?? selectedDesign.instance_count)} label="实例" />
              <StatCard value={String(detail.summary.nets ?? selectedDesign.net_count)} label="网络" />
              <StatCard value={String((detail.summary.modules as unknown[])?.length ?? selectedDesign.module_count)} label="模块" />
              <StatCard value={String(detail.summary.status ?? selectedDesign.status)} label="状态" />
            </div>
          )}
          {selectedDesign && (
            <div className="grid gap-[10px]">
              <DataSection title="模块" columns={moduleColumns} rows={detail?.modules ?? []} loading={detailLoading} />
              <div className="grid gap-[10px]">
                <FilterBar
                  title="网络"
                  value={netQuery}
                  placeholder="VCC3V3 / GND / CLK"
                  onChange={onNetQueryChange}
                  onSearch={onRefresh}
                />
                <DataSection columns={netColumns} rows={detail?.nets ?? []} loading={detailLoading} emptyText="没有匹配的网络" />
              </div>
              <div className="grid gap-[10px]">
                <FilterBar
                  title="实例"
                  value={instanceQuery}
                  placeholder="U100 / TPS / STM32"
                  onChange={onInstanceQueryChange}
                  onSearch={onRefresh}
                />
                <DataSection columns={instanceColumns} rows={detail?.instances ?? []} loading={detailLoading} emptyText="没有匹配的实例" />
              </div>
              <DataSection title="EDF/PDF 对应" columns={refColumns} rows={detail?.cross_references ?? []} loading={detailLoading} emptyText="尚未建立 EDF/PDF 对应关系" />
            </div>
          )}
          {isFailedOnly && <EmptyState text="该设计只有解析日志，未写出结构化设计状态" compact />}
          <LogPanel log={parseLog} loading={parseLogLoading} />
        </>
      )}
    </div>
  );
}

function ModulePanel({ rows, loading, onRefresh }: { rows: Record<string, unknown>[]; loading: boolean; onRefresh: () => void }) {
  const columns: DataTableColumn<Record<string, unknown>>[] = [
    { key: 'design_id', title: '设计', width: 140, render: (row) => <TextCell value={valueOf(row, 'design_id')} strong /> },
    { key: 'name', title: '模块', render: (row) => <TextCell value={valueOf(row, 'name')} /> },
    { key: 'module_id', title: '模块 ID', render: (row) => <TextCell value={valueOf(row, 'module_id')} /> },
    { key: 'instances', title: '实例', width: 80, align: 'right', render: (row) => valueOf(row, 'instance_count') },
    { key: 'nets', title: '网络', width: 80, align: 'right', render: (row) => valueOf(row, 'net_count') },
    { key: 'strategy', title: '策略', width: 120, render: (row) => valueOf(row, 'strategy') },
  ];
  return (
    <div className="grid gap-[16px]">
      <PanelToolbar title="模块树浏览" onRefresh={onRefresh} loading={loading} />
      <DataSection columns={columns} rows={rows} loading={loading} emptyText="当前知识库尚无可浏览模块" />
    </div>
  );
}

function TestDataPanel({
  reports,
  measurements,
  loading,
  query,
  onQueryChange,
  onRefresh,
}: {
  reports: Record<string, unknown>[];
  measurements: Record<string, unknown>[];
  loading: boolean;
  query: string;
  onQueryChange: (value: string) => void;
  onRefresh: () => void;
}) {
  const reportColumns: DataTableColumn<Record<string, unknown>>[] = [
    { key: 'report', title: '报告', render: (row) => <TextCell value={valueOf(row, 'title')} strong /> },
    { key: 'report_id', title: 'ID', render: (row) => <TextCell value={valueOf(row, 'report_id')} /> },
    { key: 'runs', title: '运行', width: 80, align: 'right', render: (row) => valueOf(row, 'run_count') },
    { key: 'cases', title: '用例', width: 80, align: 'right', render: (row) => valueOf(row, 'case_count') },
  ];
  const measurementColumns: DataTableColumn<Record<string, unknown>>[] = [
    { key: 'report_id', title: '报告', width: 140, render: (row) => <TextCell value={valueOf(row, 'report_id')} strong /> },
    { key: 'case', title: '用例', render: (row) => <TextCell value={valueOf(row, 'case')} /> },
    { key: 'measurement', title: '测量项', render: (row) => <TextCell value={valueOf(row, 'measurement')} /> },
    { key: 'value', title: '值', width: 110, align: 'right', render: (row) => valueOf(row, 'value') },
    { key: 'unit', title: '单位', width: 80, render: (row) => valueOf(row, 'unit') },
    { key: 'pass_fail', title: '结果', width: 80, render: (row) => <StatusText status={String(valueOf(row, 'pass_fail') ?? '')} label={String(valueOf(row, 'pass_fail') ?? '-')} /> },
  ];
  return (
    <div className="grid gap-[16px]">
      <PanelToolbar title="测试数据浏览" onRefresh={onRefresh} loading={loading} />
      <DataSection title="测试报告" columns={reportColumns} rows={reports} loading={loading} emptyText="当前知识库尚未上传测试报告" />
      <div className="grid gap-[10px]">
        <FilterBar title="测量值" value={query} placeholder="VOUT / efficiency / 3V3" onChange={onQueryChange} onSearch={onRefresh} />
        <DataSection columns={measurementColumns} rows={measurements} loading={loading} emptyText="没有匹配的测量值" />
      </div>
    </div>
  );
}

export const MESSAGE_COLLAPSE_THRESHOLD = 120;

/** One conversation message. Long content collapses by default; click to toggle. */
export function ChatMessageBubble({ role, content, ts }: { role: string; content: string; ts?: string }) {
  const [expanded, setExpanded] = useState(false);
  const isLong = content.length > MESSAGE_COLLAPSE_THRESHOLD;
  const showFull = expanded || !isLong;
  return (
    <div
      className={cn(
        'max-w-[86%] rounded-[10px] border px-[12px] py-[8px] text-[12px] leading-[18px]',
        role === 'user'
          ? 'justify-self-start cursor-pointer border-[#e3e7f1] bg-white text-[#18181a]'
          : role === 'assistant'
            ? 'justify-self-end cursor-pointer border-[#dcebdd] bg-[#f2f9f3] text-[#1f3a26]'
            : 'justify-self-start border-[#e3e7f1] bg-white text-[#464c5e]',
      )}
      onClick={isLong ? () => setExpanded((v) => !v) : undefined}
      title={isLong ? (expanded ? '点击收起' : '点击展开') : undefined}
    >
      {ts && <div className="mb-[2px] text-[11px] text-[#757f9c]">{ts}</div>}
      <div
        className={cn(
          'whitespace-pre-wrap break-words',
          !showFull && 'line-clamp-3',
        )}
      >
        {content}
      </div>
      {isLong && (
        <div className="mt-[2px] text-right text-[11px] text-[#757f9c]">
          {expanded ? '收起 ▴' : `展开 ▾ (${content.length} 字)`}
        </div>
      )}
    </div>
  );
}

export function ChatsPanel({
  items,
  loading,
  selectedId,
  detail,
  canWrite,
  summaryGenerating,
  onSelectedIdChange,
  onRefresh,
  onDelete,
  onRegenerateSummary,
}: {
  items: ExternalConversationListItem[];
  loading: boolean;
  selectedId: string;
  detail: ExternalConversationDetailResponse | null;
  canWrite: boolean;
  summaryGenerating: boolean;
  onSelectedIdChange: (value: string) => void;
  onRefresh: () => void;
  onDelete: (item: ExternalConversationListItem) => void;
  onRegenerateSummary: () => void;
}) {
  const columns: DataTableColumn<ExternalConversationListItem>[] = [
    { key: 'title', title: '会话', render: (row) => <TextCell value={row.title} strong /> },
    { key: 'source_file', title: '来源文件', render: (row) => <TextCell value={row.source_file} /> },
    { key: 'origin', title: '来源', width: 90, render: (row) => (row.origin === 'chat_deposit' ? '对话沉淀' : '上传') },
    { key: 'turns', title: '消息数', width: 80, align: 'right', render: (row) => row.turn_count || row.block_count },
    {
      key: 'actions',
      title: '',
      width: 70,
      render: (row) =>
        canWrite ? (
          <button
            type="button"
            className="text-[12px] text-[#c0392b] hover:underline"
            onClick={(event) => {
              event.stopPropagation();
              onDelete(row);
            }}
          >
            删除
          </button>
        ) : null,
    },
  ];
  const turns = detail?.turns ?? [];
  return (
    <div className="grid gap-[16px]">
      <PanelToolbar title="外部对话浏览" onRefresh={onRefresh} loading={loading} />
      <DataTable
        columns={columns}
        data={items}
        rowKey={(row) => row.conversation_id}
        size="compact"
        onRowClick={(row) => onSelectedIdChange(row.conversation_id)}
        emptyText="当前知识库尚未上传外部对话记录"
      />
      {selectedId && (
        <div className="grid gap-[10px] rounded-[12px] border border-[#e3e7f1] bg-[#fafbfc] p-[14px]">
          <div className="text-[12px] font-semibold text-[#464c5e]">
            对话内容{detail ? ` · ${detail.title}` : ''}
            {canWrite && detail && (
              <button
                type="button"
                disabled={summaryGenerating}
                onClick={onRegenerateSummary}
                className="ml-[10px] text-[11px] font-normal text-[#2563eb] hover:underline disabled:text-[#858b9c]"
              >
                {summaryGenerating ? '生成中…' : '生成/刷新 AI 摘要'}
              </button>
            )}
          </div>
          {detail?.summary && (
            <div className="rounded-[10px] border border-[#dcebdd] bg-[#f2f9f3] px-[12px] py-[10px]">
              <div className="mb-[4px] flex items-center justify-between">
                <span className="text-[11px] font-semibold text-[#1f3a26]">AI 提取摘要</span>
                {detail.summary_generated_at && (
                  <span className="text-[11px] text-[#757f9c]">{detail.summary_generated_at}</span>
                )}
              </div>
              <div className="whitespace-pre-wrap break-words text-[12px] leading-[18px] text-[#1f3a26]">
                {detail.summary}
              </div>
              {(detail.key_points?.length ?? 0) > 0 && (
                <ul className="mt-[6px] grid gap-[2px]">
                  {detail.key_points!.map((point, i) => (
                    <li key={i} className="list-inside list-disc text-[12px] leading-[18px] text-[#2c4633]">
                      {point}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}
          {!detail && loading && <PanelSkeleton rows={2} />}
          {detail && turns.length === 0 && (detail.blocks?.length ?? 0) > 0 && (
            <EmptyState text="该文件未解析出对话轮次,以下为按话题分块的内容" />
          )}
          {detail && turns.length === 0 && (detail.blocks?.length ?? 0) === 0 && (
            <EmptyState text="该文件暂无可展示的内容" />
          )}
          {(detail?.blocks ?? []).map((block) => (
            <ChatMessageBubble
              key={`${detail?.conversation_id ?? 'd'}:b${String(block?.index ?? '')}`}
              role="document"
              content={String(block?.content ?? '')}
            />
          ))}
          {turns.map((turn, index) => (
            <ChatMessageBubble
              key={`${detail?.conversation_id ?? 'd'}:t${index}`}
              role={turn.role}
              content={turn.content}
              ts={turn.ts}
            />
          ))}
          {detail?.preview && turns.length === 0 && (
            <pre className="max-h-[240px] overflow-auto whitespace-pre-wrap break-words rounded-[8px] bg-white p-[10px] text-[12px] leading-[18px] text-[#464c5e]">
              {detail.preview}
            </pre>
          )}
        </div>
      )}
    </div>
  );
}

function SchematicPanel({
  data,
  loading,
  selectedDesignId,  onSelectedDesignIdChange,
  selectedPageNumber,
  onSelectedPageNumberChange,
  page,
  pageLoading,
  onRefresh,
}: {
  data: SchematicDesignsResponse | null;
  loading: boolean;
  selectedDesignId: string;
  onSelectedDesignIdChange: (value: string) => void;
  selectedPageNumber: number | null;
  onSelectedPageNumberChange: (value: number | null) => void;
  page: SchematicPageResponse | null;
  pageLoading: boolean;
  onRefresh: () => void;
}) {
  const designs = data?.designs ?? [];
  const selected = designs.find((row) => row.design_id === selectedDesignId) ?? designs[0];
  const labelColumns: DataTableColumn<Record<string, unknown>>[] = [
    { key: 'text', title: '文本', render: (row) => <TextCell value={valueOf(row, 'text')} strong /> },
    { key: 'kind', title: '类型', width: 100, render: (row) => valueOf(row, 'kind') },
    { key: 'bbox', title: '位置', render: (row) => <TextCell value={valueOf(row, 'bbox')} /> },
  ];
  const regionColumns: DataTableColumn<Record<string, unknown>>[] = [
    { key: 'module', title: '模块', render: (row) => <TextCell value={valueOf(row, 'module_id')} strong /> },
    { key: 'bbox', title: '区域', render: (row) => <TextCell value={valueOf(row, 'bbox')} /> },
    { key: 'confidence', title: '置信度', width: 90, align: 'right', render: (row) => formatPercent(valueOf(row, 'confidence')) },
    { key: 'strategy', title: '策略', width: 120, render: (row) => valueOf(row, 'strategy') },
  ];
  return (
    <div className="grid gap-[16px]">
      <PanelToolbar title="原理图查看" onRefresh={onRefresh} loading={loading || pageLoading} />
      {loading && !data ? (
        <PanelSkeleton rows={3} />
      ) : designs.length === 0 ? (
        <EmptyState text="当前知识库尚无原理图 PDF 解析结果" />
      ) : (
        <>
          <div className="flex flex-wrap items-end gap-[10px]">
            <div className="grid min-w-[260px] gap-[4px]">
              <span className="text-[11px] text-[#858b9c]">设计</span>
              <Select value={selected?.design_id ?? ''} onValueChange={onSelectedDesignIdChange}>
                <SelectTrigger className="h-[34px] w-full rounded-[10px] border-[#e3e7f1] text-[12px]">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {designs.map((row) => (
                    <SelectItem key={row.design_id} value={row.design_id}>{row.design_id} · {row.page_count} 页</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="grid min-w-[160px] gap-[4px]">
              <span className="text-[11px] text-[#858b9c]">页码</span>
              <Select
                value={selectedPageNumber == null ? '' : String(selectedPageNumber)}
                onValueChange={(value) => onSelectedPageNumberChange(Number(value))}
              >
                <SelectTrigger className="h-[34px] w-full rounded-[10px] border-[#e3e7f1] text-[12px]">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {(selected?.pages ?? []).map((item) => (
                    <SelectItem key={item.page_number} value={String(item.page_number)}>第 {item.page_number} 页</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          </div>
          {pageLoading ? (
            <PanelSkeleton rows={2} />
          ) : page ? (
            <>
              <div className="grid grid-cols-4 gap-[10px] max-[900px]:grid-cols-2">
                <StatCard value={page.labels.length} label="标签" />
                <StatCard value={page.module_regions.length} label="模块区域" />
                <StatCard value={page.width ? Math.round(page.width) : '-'} label="宽度" />
                <StatCard value={page.height ? Math.round(page.height) : '-'} label="高度" />
              </div>
              <div className="rounded-[10px] border border-[#f0f1f4] bg-[#fafbfc] p-[12px]">
                <h3 className="mb-[8px] text-[13px] font-semibold text-[#18181a]">提取文本</h3>
                <pre className="max-h-[220px] overflow-auto whitespace-pre-wrap break-words text-[12px] leading-[18px] text-[#464c5e]">{page.text || '该页未提取到文本'}</pre>
              </div>
              <DataSection title="文本标签" columns={labelColumns} rows={page.labels} emptyText="暂无文本标签" />
              <DataSection title="候选模块区域" columns={regionColumns} rows={page.module_regions} emptyText="暂无候选模块区域" />
            </>
          ) : (
            <EmptyState text="请选择原理图页面" compact />
          )}
        </>
      )}
    </div>
  );
}

function PanelToolbar({ title, onRefresh, loading }: { title: string; onRefresh: () => void; loading?: boolean }) {
  return (
    <div className="flex flex-wrap items-center justify-between gap-[10px]">
      <h2 className="text-[15px] font-semibold text-[#18181a]">{title}</h2>
      <Button variant="outline" className={cn(OUTLINE_ACTION_BUTTON_CLASS, 'h-[30px] px-[10px]')} onClick={onRefresh} disabled={loading}>
        <AppIcon name="refresh" size={13} />
        刷新
      </Button>
    </div>
  );
}

function DataSection({
  title,
  columns,
  rows,
  loading,
  emptyText = '暂无数据',
}: {
  title?: string;
  columns: DataTableColumn<Record<string, unknown>>[];
  rows: Record<string, unknown>[];
  loading?: boolean;
  emptyText?: string;
}) {
  return (
    <div className="grid gap-[10px]">
      {title && <h3 className="text-[14px] font-semibold text-[#18181a]">{title}</h3>}
      {loading ? (
        <PanelSkeleton rows={2} />
      ) : (
        <DataTable columns={columns} data={rows} rowKey={(row, index) => rowKey(row, index)} size="compact" emptyText={emptyText} />
      )}
    </div>
  );
}

function FilterBar({
  title,
  value,
  placeholder,
  onChange,
  onSearch,
}: {
  title: string;
  value: string;
  placeholder: string;
  onChange: (value: string) => void;
  onSearch: () => void;
}) {
  return (
    <div className="flex flex-wrap items-center justify-between gap-[10px]">
      <h3 className="text-[14px] font-semibold text-[#18181a]">{title}</h3>
      <div className="flex min-w-[260px] max-w-full gap-[8px]">
        <Input
          value={value}
          onChange={(event) => onChange(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter') onSearch();
          }}
          placeholder={placeholder}
          className="h-[32px] rounded-[10px] border-[#e3e7f1] text-[12px]"
        />
        <Button variant="outline" className={cn(OUTLINE_ACTION_BUTTON_CLASS, 'h-[32px] px-[10px]')} onClick={onSearch}>
          <AppIcon name="search" size={13} />
        </Button>
      </div>
    </div>
  );
}

function LogPanel({ log, loading }: { log: CircuitParseLogResponse | null; loading: boolean }) {
  if (loading) return <PanelSkeleton rows={1} />;
  return (
    <div className="rounded-[10px] border border-[#f0f1f4] bg-[#fafbfc] p-[12px]">
      <div className="mb-[8px] flex flex-wrap items-center justify-between gap-[8px]">
        <h3 className="text-[13px] font-semibold text-[#18181a]">解析日志</h3>
        <span className="truncate text-[11px] text-[#858b9c]">{log?.path || '-'}</span>
      </div>
      {log?.exists ? (
        <pre className="max-h-[260px] overflow-auto whitespace-pre-wrap break-words text-[12px] leading-[18px] text-[#464c5e]">{log.content}</pre>
      ) : (
        <div className="py-[16px] text-center text-[12px] text-[#858b9c]">尚无解析日志</div>
      )}
    </div>
  );
}

function PanelSkeleton({ rows = 2 }: { rows?: number }) {
  return (
    <div className="grid gap-[10px]">
      {Array.from({ length: rows }).map((_, index) => <Skeleton key={index} className="h-[46px] rounded-[10px]" />)}
    </div>
  );
}

function EmptyState({ text, compact = false }: { text: string; compact?: boolean }) {
  return (
    <div className={cn('flex flex-col items-center gap-[10px] text-[#858b9c]', compact ? 'py-[22px]' : 'py-[48px]')}>
      <span className="text-[13px]">{text}</span>
    </div>
  );
}

function StatusText({ status, label }: { status: string; label: string }) {
  const normalized = (status || '').toLowerCase();
  const className = normalized === 'failed' || normalized === 'fail' ? 'text-[#d20b0b]' : normalized === 'pass' || normalized === 'completed' ? 'text-[#2cb360]' : 'text-[#464c5e]';
  return <span className={className}>{label || '-'}</span>;
}

function TextCell({ value, strong = false }: { value: unknown; strong?: boolean }) {
  return <span className={cn('block truncate', strong ? 'font-medium text-[#18181a]' : 'text-[#464c5e]')}>{formatCellValue(value)}</span>;
}

function valueOf(row: Record<string, unknown>, key: string): string {
  return formatCellValue(row[key]);
}

function formatCellValue(value: unknown): string {
  if (value == null || value === '') return '-';
  if (Array.isArray(value)) return value.map((item) => formatCellValue(item)).join(', ');
  if (typeof value === 'object') return JSON.stringify(value);
  return String(value);
}

function formatPercent(value: unknown): string {
  const n = Number(value);
  if (!Number.isFinite(n)) return '-';
  return `${Math.round(n * 100)}%`;
}

function rowKey(row: Record<string, unknown>, index: number): string {
  return String(row.id ?? row.module_id ?? row.name ?? row.refdes ?? row.report_id ?? row.design_id ?? index);
}

function formatTaskTime(value?: number | null): string {
  if (value == null) return '-';
  return new Date(value * 1000).toLocaleString();
}

function ParseTaskRow({
  task,
  onDelete,
}: {
  task: ParseTaskView;
  onDelete: (task: ParseTaskView) => void;
}) {
  const progress = Math.max(0, Math.min(100, Number(task.progress) || 0));
  return (
    <div className="grid gap-[8px] rounded-[10px] border border-[#f0f1f4] bg-[#fafbfc] px-[12px] py-[10px]">
      <div className="flex flex-wrap items-center gap-[8px]">
        <span className="min-w-0 flex-1 truncate text-[13px] font-medium text-[#18181a]">
          {task.original_name || task.source_path || task.id}
        </span>
        <span className="rounded-full bg-[#f3f4f6] px-[8px] py-[2px] text-[11px] text-[#464c5e]">
          {task.status || '-'}
        </span>
        <button type="button" onClick={() => onDelete(task)} className="text-[12px] text-[#d20b0b]">
          停止并删除
        </button>
      </div>
      <div className="h-[6px] overflow-hidden rounded-full bg-[#eceef1]">
        <div className="h-full rounded-full bg-[#18181a]" style={{ width: `${progress}%` }} />
      </div>
      <div className="flex flex-wrap justify-between gap-[8px] text-[11px] text-[#858b9c]">
        <span>{task.stage || task.message || '-'}</span>
        <span>{progress}% · 更新 {formatTaskTime(task.updated_at)}</span>
      </div>
    </div>
  );
}
