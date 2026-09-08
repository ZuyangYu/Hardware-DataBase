/**
 * documentGenerationDeepLink -- 工作台深链解析的纯函数模块。
 *
 * 解析优先级:task 是权威身份(由聚合投影反查会话/计划/工单);
 * session 次之;workOrder 保留原有直连视图。task 与任一引用并存时以
 * task 为准并由服务端聚合校验;session 与 workOrder 并存且无 task 属于
 * 身份冲突,必须显式报错而不是静默选择其一。
 */

export type DeepLinkRefs = {
  kb: string;
  task: string;
  session: string;
  workOrder: string;
};

export type ResolvedDeepLink =
  | { kind: 'task'; taskId: string }
  | { kind: 'session'; sessionId: string }
  | { kind: 'workOrder'; workOrderId: string }
  | { kind: 'conflict'; refs: DeepLinkRefs }
  | { kind: 'none' };

function nonEmpty(value: string | null | undefined): string {
  return typeof value === 'string' ? value.trim() : '';
}

/** URLSearchParams → 引用集;URLSearchParams 自带百分号解码。 */
export function parseDocumentGenerationDeepLink(
  searchParams: URLSearchParams,
): DeepLinkRefs {
  return {
    kb: nonEmpty(searchParams.get('kb')),
    task: nonEmpty(searchParams.get('task')),
    session: nonEmpty(searchParams.get('session')),
    workOrder: nonEmpty(searchParams.get('workOrder')),
  };
}

export function resolveDocumentGenerationDeepLink(
  refs: DeepLinkRefs,
): ResolvedDeepLink {
  if (refs.task) return { kind: 'task', taskId: refs.task };
  const hasSession = Boolean(refs.session);
  const hasWorkOrder = Boolean(refs.workOrder);
  if (hasSession && hasWorkOrder) return { kind: 'conflict', refs };
  if (hasSession) return { kind: 'session', sessionId: refs.session };
  if (hasWorkOrder) return { kind: 'workOrder', workOrderId: refs.workOrder };
  return { kind: 'none' };
}
