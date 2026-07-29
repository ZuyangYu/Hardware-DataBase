import { formatClientDateTime } from './timezone';

/**
 * 共享 Tailwind class token(企业列表/对话框/菜单样式),照搬 enterprise-ui。
 * 集中维护,避免每个页面复制粘贴同一套 dropdown/select/card 样式。
 */

/** 标准 outline 操作按钮(工具条刷新、卡片操作等)。 */
export const OUTLINE_ACTION_BUTTON_CLASS =
  'h-[34px] gap-[4px] rounded-[10px] border-[0.5px] border-[#e3e7f1] bg-white px-[20px] text-[12px] font-normal text-[#757f9c] hover:border-[#cbd3e6] hover:bg-white hover:text-[#18181a]';

/** 把后端时间戳格式化成当前 locale 显示,空/非法返回 `-`。 */
export function formatDateTime(value?: string): string {
  return formatClientDateTime(value, '-');
}
