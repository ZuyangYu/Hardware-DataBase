/**
 * 聊天头部(对齐 ChatHeader 结构:会话标题 + 右侧用户菜单下拉退出)。
 * 删掉重命名按钮/员工 handoff;保留会话标题 + KB 名 + 退出。
 */
import UserMenu from '@/components/UserMenu';
import {
  CHAT_HEADER_CLASS,
  CHAT_HEADER_TITLE_NAME_CLASS,
  CHAT_HEADER_TITLE_STACK_CLASS,
  CHAT_HEADER_TITLE_META_CLASS,
} from '../chatPageStyles';

type Props = {
  title: string;
  kbName: string;
  userName: string;
  onLogout: () => void;
};

export default function ChatHeader({ title, kbName, userName, onLogout }: Props) {
  return (
    <div className={CHAT_HEADER_CLASS}>
      <div className={CHAT_HEADER_TITLE_STACK_CLASS}>
        <span className={CHAT_HEADER_TITLE_NAME_CLASS}>{title || '新对话'}</span>
        <span className={CHAT_HEADER_TITLE_META_CLASS}>{kbName}</span>
      </div>
      <UserMenu userName={userName} onLogout={onLogout} />
    </div>
  );
}
