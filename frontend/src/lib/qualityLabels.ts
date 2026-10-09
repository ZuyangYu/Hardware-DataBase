/**
 * 质量案例共享标签与文案的单一来源。
 * 后台工作台(QualityPage/QualityWorkflowPanel)与聊天反馈组件
 * (FeedbackActions)统一从这里取,页面不再反向依赖聊天组件;
 * 值与色调集中维护,避免各处硬编码中文字典漂移。
 */
import type { QualityReason } from '@/api/types';
import type { StatusTone } from '@/components/admin/StatusBadge';

export const QUALITY_REASONS: Record<QualityReason, string> = {
  factual_error: '事实错误', unsupported_evidence: '证据不支持', not_found: '资料未找到',
  version_conflict: '资料版本冲突', incomplete_task: '未完成任务', format_mismatch: '格式不符',
};

export const QUALITY_STATUS: Record<string, string> = {
  new: '待复核', triaged: '已分派', reviewed: '已确认问题', needs_information: '待补充材料',
  rejected: '复核未发现问题', dataset_ready: '已入回归集', regression_verified: '回归已验证', closed: '已上线验证',
};

/** 状态徽章色调:颜色仅辅助,中文文案始终存在。 */
export const QUALITY_STATUS_TONE: Record<string, StatusTone> = {
  new: 'warning', triaged: 'info', reviewed: 'info', needs_information: 'warning',
  rejected: 'neutral', dataset_ready: 'info', regression_verified: 'success', closed: 'success',
};

export const QUALITY_JUDGMENTS: Record<string, string> = {
  confirmed: '确认问题', not_an_error: '未发现错误', needs_information: '需要补充材料',
};

export const QUALITY_CORRECTNESS: Record<string, string> = {
  correct: '已确认事实正确', incorrect: '已确认事实错误', not_evaluated: '尚未判定',
};

export const QUALITY_ROOT_CAUSES: Record<string, string> = {
  unknown: '待确定', retrieval: '检索', parsing: '解析', missing_source: '资料缺失',
  prompt: 'Prompt', model: '模型', tool: '工具', permission: '权限', presentation: '前端呈现',
};

/** 根因 -> 修复动作:管理员不读正文时的行动建议。 */
export const ROOT_CAUSE_FIXES: Record<string, { label: string; action: string }> = {
  retrieval: { label: '检索', action: '检查召回范围、证据排序和无关材料过滤，修复后用同一固定问题重新检索验证。' },
  parsing: { label: '解析', action: '检查文件解析、表格/原理图索引和字段映射，修复解析结果后重新生成证据。' },
  missing_source: { label: '资料缺失', action: '补充或更新授权知识库资料，记录资料版本，再用同一问题重新回答。' },
  prompt: { label: 'Prompt', action: '调整回答约束、格式要求或缺失信息披露规则，再运行固定数据集对比。' },
  model: { label: '模型', action: '调整模型或参数，并用相同冻结输入执行基线/候选配对评测。' },
  tool: { label: '工具', action: '检查工具调用参数、错误处理和返回内容，修复后重新执行受影响任务。' },
  permission: { label: '权限', action: '检查知识库、部门和共享授权，确认修复后不会扩大数据访问范围。' },
  presentation: { label: '前端呈现', action: '检查回答格式、引用展示、字段完整性和前端渲染规则，再用同一回答标准回归。' },
  unknown: { label: '待确定', action: '先由原用户或同部门授权员工查看固定提问、回答和证据，补充复核结论后再确定修复方向。' },
};

/** 案例状态 -> 下一阶段说明。 */
export const WORKFLOW_STAGES: Record<string, { label: string; next: string }> = {
  new: { label: '待复核', next: '由原用户共享选定材料，安排同部门员工完成问题判断和根因复核。' },
  triaged: { label: '已分派', next: '安排已获授权的员工复核固定材料，记录问题判断和根因。' },
  dataset_ready: { label: '已入回归集', next: '用冻结数据集执行基线与候选配对评测，确认候选通过门禁。' },
  needs_information: { label: '待补充材料', next: '联系原用户补充提问、回答或证据授权，然后重新提交复核。' },
  reviewed: { label: '已确认问题', next: '原回答 owner 冻结案例，实施修复并运行基线/候选配对评测。' },
  regression_verified: { label: '回归已验证', next: '确认发布 revision 和部署时间，在线上版本执行同一问题回访。' },
  closed: { label: '已上线验证', next: '闭环完成；继续通过统计报告观察同类反馈和费用影响。' },
  rejected: { label: '复核未发现问题', next: '保留复核依据；如用户仍有异议，由原用户补充新的反馈材料。' },
};

export const QUALITY_TRIGGER_LABELS: Record<string, string> = {
  feedback: '用户反馈', random_sample: '随机抽样', diagnostic: '诊断事件',
};

export const QUALITY_FEEDBACK_RATING_LABELS: Record<string, string> = {
  helpful: '有帮助', unhelpful: '没帮助', withdrawn: '已撤销',
};

export const QUALITY_CONTENT_ACCESS_LABELS: Record<string, string> = {
  owner: '本人材料', shared: '授权副本', hidden: '仅元数据',
};

/** 内容访问权限徽章色调。 */
export const QUALITY_CONTENT_ACCESS_TONE: Record<string, StatusTone> = {
  owner: 'success', shared: 'info', hidden: 'readonly',
};

/** 用户反馈结果徽章色调。 */
export const QUALITY_FEEDBACK_RATING_TONE: Record<string, StatusTone> = {
  helpful: 'success', unhelpful: 'danger', withdrawn: 'neutral',
};

/** 技术记录投影(Phoenix)状态中文标签。 */
export const QUALITY_PROJECTION_LABELS: Record<string, string> = {
  completed: '已同步', pending: '待投递', running: '投递中', failed: '投递失败',
  disabled: '同步未开启', no_span: '无采样 span',
};

export const QUALITY_RECOMMENDATION_LABELS: Record<string, string> = {
  proposed: '待实验验证', verified: '已验证', in_progress: '验证中', rejected: '未通过验证',
};
