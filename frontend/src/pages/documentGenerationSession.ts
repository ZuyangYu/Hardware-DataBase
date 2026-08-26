import type {
  ClarificationMessage,
  GenerationBriefView,
  GenerationSession,
} from '../api/types';
import type { ClarificationMessageView } from './documentGenerationWorkbench';

export function sessionMessagesForWorkbench(session: GenerationSession): ClarificationMessageView[] {
  return session.messages.map((message) => ({
    id: message.message_id,
    role: message.role,
    content: message.content,
    options: message.options ?? [],
  }));
}

export function latestClarificationQuestion(session: GenerationSession): ClarificationMessage | undefined {
  return [...session.messages].reverse().find((message) => (
    message.role === 'assistant' && Boolean(message.question_id)
  ));
}

export function confirmedBriefItems(brief: GenerationBriefView): string[] {
  const items: string[] = [];
  const revision = String(brief.scope.revision ?? '').trim();
  if (brief.purpose.trim()) items.push(`文档用途：${brief.purpose.trim()}`);
  if (revision) items.push(`项目版本：${revision}`);
  if (brief.missing_data_policy) items.push(`缺失数据：${brief.missing_data_policy}`);
  if (brief.inference_policy) items.push(`AI 推断：${brief.inference_policy}`);
  const format = String(brief.output_policy.format ?? '').trim();
  if (format) items.push(`输出格式：${format}`);
  return items;
}

export function pendingBriefItems(brief: GenerationBriefView): string[] {
  const items: string[] = [];
  if (!String(brief.scope.revision ?? '').trim()) items.push('项目版本');
  if (!brief.missing_data_policy) items.push('缺失数据处理');
  if (!brief.inference_policy) items.push('AI 推断策略');
  return items;
}
