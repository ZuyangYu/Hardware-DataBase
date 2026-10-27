/**
 * 聊天空态:硬件数据工作区的入口页。
 * 保留问候、检索范围和示例问题;只调整视觉层级,不改交互。
 */
import AppIcon from '@/components/AppIcon';

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
    <div className="mx-auto flex w-full max-w-[720px] flex-col gap-[18px] pt-[7vh]">
      <section className="overflow-hidden rounded-[24px] border border-[#eceef3] bg-white shadow-[0_12px_40px_rgba(24,24,26,0.05)]">
        <div className="flex items-start gap-[16px] px-[26px] pb-[22px] pt-[26px]">
          <span className="inline-grid size-[44px] shrink-0 place-items-center rounded-[14px] bg-[#18181a] text-white shadow-[0_8px_18px_rgba(24,24,26,0.18)]">
            <AppIcon name="database" size={21} />
          </span>
          <div className="min-w-0">
            <div className="text-[10px] font-medium uppercase tracking-[0.14em] text-[#9aa1b1]">
              Hardware DataBase
            </div>
            <h1 className="mt-[3px] text-[28px] font-semibold leading-[1.2] tracking-[-0.02em] text-[#18181a]">
              Hello {userName}!
            </h1>
            <p className="mt-[7px] text-[13px] leading-[20px] text-[#6b7280]">
              {mounted
                ? `当前检索范围：${kbName}。支持文档、表格、电路与附件多源检索。`
                : '当前为通用对话，不读取知识库资料；你仍然可以直接提问。'}
            </p>
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-[7px] border-t border-[#f0f2f6] bg-[#fafbfc] px-[26px] py-[12px]">
          {['文档', '表格', '电路', '附件', '引用追溯'].map((tag) => (
            <span
              key={tag}
              className="rounded-full border border-[#e6e9f0] bg-white px-[9px] py-[3px] text-[10px] font-medium text-[#6b7280]"
            >
              {tag}
            </span>
          ))}
          <span className="ml-auto hidden text-[10px] text-[#a1a7b5] sm:inline">
            Enter 发送 · Shift+Enter 换行
          </span>
        </div>
      </section>

      <section className="rounded-[20px] border border-[#eceef3] bg-white p-[18px] shadow-[0_1px_3px_rgba(24,24,26,0.04)]">
        <div className="mb-[12px] flex items-end justify-between gap-[12px]">
          <div>
            <div className="text-[13px] font-semibold text-[#18181a]">试试这些问题</div>
            <div className="mt-[2px] text-[11px] text-[#9aa1b1]">点击后填入输入框，可继续修改</div>
          </div>
          {mounted && (
            <span className="inline-flex items-center gap-[5px] rounded-full bg-[#f4f5f7] px-[8px] py-[3px] text-[10px] text-[#6b7280]">
              <span className="size-[5px] rounded-full bg-[#8b93a7]" aria-hidden />
              {kbName}
            </span>
          )}
        </div>
        {mounted && onPickSuggestion ? (
          <div className="grid gap-[8px] sm:grid-cols-2">
            {suggestions.map((text) => (
              <button
                key={text}
                type="button"
                onClick={() => onPickSuggestion(text)}
                className="group flex min-h-[52px] items-center justify-between gap-[10px] rounded-[12px] border border-[#eceef3] bg-[#fafbfc] px-[13px] py-[10px] text-left text-[12px] leading-[18px] text-[#464c5e] transition-all hover:border-[#d5dae3] hover:bg-white hover:text-[#18181a] hover:shadow-[0_2px_8px_rgba(24,24,26,0.05)]"
              >
                <span>{text}</span>
                <AppIcon
                  name="arrow"
                  size={13}
                  className="shrink-0 text-[#c3c8d4] transition-transform group-hover:translate-x-[2px] group-hover:text-[#18181a]"
                />
              </button>
            ))}
          </div>
        ) : (
          <div className="rounded-[12px] border border-dashed border-[#e6e9f0] px-[14px] py-[18px] text-center text-[11px] leading-[18px] text-[#9aa1b1]">
            选择左侧项目知识库后，这里会显示可用的示例问题。
          </div>
        )}
      </section>
    </div>
  );
}
