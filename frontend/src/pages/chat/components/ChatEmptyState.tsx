/**
 * 聊天空态(对齐 ChatEmptyState,去掉员工资料卡,改成 HD 版问候 + 提示)。
 * 员工头像/角色摘要/标签/统计 -> 问候 + KB 提示卡。
 */
import {
  CHAT_EMPTY_CARD_CLASS,
  CHAT_EMPTY_CLASS,
  CHAT_EMPTY_GREETING_CARD_CLASS,
  CHAT_EMPTY_STAT_CELL_CLASS,
  CHAT_EMPTY_SUBTITLE_CLASS,
  CHAT_EMPTY_TITLE_CLASS,
} from '../chatPageStyles';

type Props = {
  userName: string;
  kbName: string;
  /** 示例问题;点击填入输入框。 */
  suggestions?: string[];
  onPickSuggestion?: (text: string) => void;
};

const DEFAULT_SUGGESTIONS = [
  '这个知识库里有哪些文档？',
  '帮我总结最新的设计资料',
  '电路中用到了哪些关键器件？',
];

export default function ChatEmptyState({ userName, kbName, suggestions = DEFAULT_SUGGESTIONS, onPickSuggestion }: Props) {
  const mounted = Boolean(kbName);
  return (
    <div className={CHAT_EMPTY_CLASS}>
      <div className={CHAT_EMPTY_GREETING_CARD_CLASS}>
        <div className="flex min-w-0 flex-col gap-[6px] py-[20px] pl-[6px]">
          <strong className={CHAT_EMPTY_TITLE_CLASS}>Hello {userName}!</strong>
          <span className={CHAT_EMPTY_SUBTITLE_CLASS}>
            {mounted ? `挂载「${kbName}」进行资产检索` : '未挂载知识库,使用通用对话'}
          </span>
        </div>
      </div>
      <div className={CHAT_EMPTY_CARD_CLASS}>
        <div className={CHAT_EMPTY_STAT_CELL_CLASS}>
          <span className="text-[11px] text-[#757f9c]">{mounted ? '提示' : '挂载状态'}</span>
          <strong className="text-[13px] font-medium text-[#18181a]">
            {mounted ? '支持文档 / 表格 / 电路多源检索' : '不读取知识库资料'}
          </strong>
        </div>
        {mounted && onPickSuggestion && (
          <div className="flex min-w-0 flex-1 flex-col justify-center gap-[8px] px-[4px]">
            <p className="text-[11px] text-[#757f9c]">试试这些问题</p>
            <div className="grid gap-[6px]">
              {suggestions.map((text) => (
                <button
                  key={text}
                  type="button"
                  onClick={() => onPickSuggestion(text)}
                  className="rounded-[8px] border border-[#e3e7f1] bg-[#fafbfc] px-[12px] py-[8px] text-left text-[12px] text-[#464c5e] transition-colors hover:border-[#c9d2e4] hover:bg-white hover:text-[#18181a]"
                >
                  {text}
                </button>
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
