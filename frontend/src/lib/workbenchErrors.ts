import { ApiError } from '@/api/client';

/** Preserve actionable Chinese errors, translate known API contracts without guessing from HTTP text. */
export function workbenchErrorMessage(error: unknown, fallback: string): string {
  const message = error instanceof Error ? error.message : '';
  if (/10000|narrow the time window|analysis window too large/i.test(message)) {
    return '数据量超过当前上限，请缩小时间范围后重试（趋势和导出最多 10000 次调用）。';
  }
  if (error instanceof ApiError) {
    if (error.status === 403) return '没有权限读取当前范围，请检查账号和部门授权。';
    if (error.status === 409) {
      if (/overlap/i.test(message)) return '同一部门、用途、模型和币种的预算有效期重叠，请调整开始或结束时间。';
      if (/budget versions are immutable/i.test(message)) return '预算版本已存在且不可修改，请使用新的预算版本号。';
      return '记录已更新或版本冲突，请刷新最新数据后重试；未保存草稿会保留。';
    }
    if (error.status === 422) return '参数无效，请检查时间范围、部门 ID、币种和金额。';
    if (error.status === 404) return '记录不存在或已不在当前可见范围内，请刷新列表。';
    if (error.status >= 500) return `${fallback}，服务暂时不可用，请稍后重试。`;
  }
  return /[\u3400-\u9fff]/.test(message) ? message : `${fallback}，请检查网络或稍后重试。`;
}
