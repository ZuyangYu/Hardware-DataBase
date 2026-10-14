import type { TicketStatus } from '@/api/types';
import type { StatusTone } from '@/components/admin/StatusBadge';

export const TICKET_STATUS: Record<TicketStatus, string> = {
  open: '待处理', in_progress: '处理中', waiting: '待补充', resolved: '已解决', closed: '已关闭',
};
export const TICKET_TONE: Record<TicketStatus, StatusTone> = {
  open: 'warning', in_progress: 'info', waiting: 'warning', resolved: 'success', closed: 'neutral',
};
export const TICKET_EVENT: Record<string, string> = {
  submit: '提交工单', supplement: '补充反馈', reply: '回复', claim: '认领工单', assign: '转派工单',
  wait: '请求补充', resolve: '提供解决办法', reopen: '重新打开', close: '关闭工单', withdraw: '撤销反馈',
};
