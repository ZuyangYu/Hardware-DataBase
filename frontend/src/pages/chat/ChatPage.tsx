/**
 * 聊天页(薄壳,对齐 ChatPage 58 行结构)。
 * Shell 内聊天布局:会话侧栏 + 聊天主区(头部 + 消息列表 + 输入区)。
 * 数据层在 useKbChat;渲染在各 chat/components 子组件。
 */
import type { CSSProperties } from 'react';
import { useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';

import type { AuthSession } from '../../auth';
import type { KbView } from '@/api/types';
import { cn } from '@/lib/utils';
import AppIcon from '@/components/AppIcon';
import { CHAT_MAIN_CLASS } from './chatPageStyles';
import { useKbChat } from './useKbChat';
import ChatSessionSidebar from './components/ChatSessionSidebar';
import ChatHeader from './components/ChatHeader';
import MessageList from './components/MessageList';
import Composer from './components/Composer';

type Props = {
  auth: AuthSession;
  kbName?: string;
  availableKbs?: KbView[];
  onLogout: () => void;
};

export default function ChatPage({ auth, kbName = '', availableKbs = [], onLogout }: Props) {
  const navigate = useNavigate();
  const [mountedKbName, setMountedKbName] = useState(kbName);
  const visibleKbs = useMemo(() => {
    const rows = availableKbs.filter((kb) => kb.permission);
    if (!mountedKbName || rows.some((kb) => kb.name === mountedKbName)) {
      return rows;
    }
    return [
      {
        name: mountedKbName,
        kb_id: null,
        department_id: null,
        department_name: null,
        permission: 'read',
        registered: true,
      },
      ...rows,
    ];
  }, [availableKbs, mountedKbName]);

  useEffect(() => {
    setMountedKbName(kbName);
  }, [kbName]);

  function handleKbChange(nextKbName: string) {
    setMountedKbName(nextKbName);
    navigate(nextKbName ? `/chat?kb=${encodeURIComponent(nextKbName)}` : '/chat', { replace: true });
  }

  const chat = useKbChat(mountedKbName);
  const {
    sessions,
    sessionsLoaded,
    activeSession,
    activeSessionId,
    selectSession,
    newConversation,
    deleteSession,
    messages,
    messagesLoaded,
    evidenceByMessageId,
    input,
    setInput,
    streaming,
    streamingText,
    traceSteps,
    send,
    abortStream,
    forbidden,
  } = chat;

  // 403 整页提示(system_admin 或无权限)
  if (forbidden) {
    return (
      <div className="flex h-full min-h-[360px] items-center justify-center bg-[#fcfcfc]">
        <div className="flex max-w-[420px] flex-col items-center gap-[12px] rounded-[16px] border border-[#e3e7f1] bg-white p-[36px] text-center shadow-[0_8px_24px_rgba(17,17,17,0.045)]">
          <AppIcon name="warning" size={36} className="text-[#b45309]" />
          <div className="text-[15px] font-semibold text-[#18181a]">无法访问该知识库</div>
          <div className="text-[13px] leading-[20px] text-[#757f9c]">{forbidden}</div>
        </div>
      </div>
    );
  }

  const sidebarProviderStyle = { '--sidebar-width': '220px', '--sidebar-width-icon': '72px' } as CSSProperties;
  const showEmpty = activeSession == null && sessionsLoaded && !streaming;

  return (
    <div className="flex h-full min-h-0 bg-[#fcfcfc] text-[#18181a]" style={sidebarProviderStyle}>
      <ChatSessionSidebar
        kbName={mountedKbName}
        availableKbs={visibleKbs}
        sessions={sessions}
        sessionsLoaded={sessionsLoaded}
        activeSessionId={activeSessionId}
        streaming={streaming}
        onKbChange={handleKbChange}
        onSelect={selectSession}
        onNew={() => void newConversation()}
        onDelete={(id) => void deleteSession(id)}
      />
      <main className={cn(CHAT_MAIN_CLASS, 'flex-1')}>
        <ChatHeader
          title={activeSession?.title || '新对话'}
          kbName={mountedKbName || '未挂载'}
          userName={auth.user.username}
          onLogout={onLogout}
        />
        <MessageList
          messages={messages}
          messagesLoaded={messagesLoaded}
          evidenceByMessageId={evidenceByMessageId}
          showEmpty={showEmpty}
          userName={auth.user.username}
          kbName={mountedKbName}
          onPickSuggestion={(text) => setInput(text)}
          streaming={streaming}
          streamingText={streamingText}
          traceSteps={traceSteps}
        />
        <Composer
          kbName={mountedKbName}
          input={input}
          setInput={setInput}
          streaming={streaming}
          onSend={() => void send()}
          onStop={abortStream}
        />
      </main>
    </div>
  );
}
