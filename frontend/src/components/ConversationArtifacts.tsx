import { useEffect, useState } from 'react';

import {
  downloadConversationArtifact,
  listConversationArtifacts,
  type ConversationArtifactView,
} from '../api/artifacts';
import { notify } from '@/components/ui/app-toast';

type Props = {
  sessionId: number | null;
  refreshKey?: string | number;
};

function formatBytes(value: number): string {
  if (!Number.isFinite(value) || value <= 0) return '0 B';
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

export default function ConversationArtifacts({ sessionId, refreshKey }: Props) {
  const [items, setItems] = useState<ConversationArtifactView[]>([]);

  useEffect(() => {
    if (sessionId == null) {
      setItems([]);
      return;
    }
    let cancelled = false;
    listConversationArtifacts(sessionId)
      .then((data) => {
        if (!cancelled) setItems(data.items);
      })
      .catch(() => {
        if (!cancelled) setItems([]);
      });
    return () => {
      cancelled = true;
    };
  }, [sessionId, refreshKey]);

  if (items.length === 0) return null;

  return (
    <div className="shrink-0 px-[24px] pb-[8px]" data-testid="conversation-artifacts">
      <div className="mx-auto w-full max-w-[820px] rounded-[10px] border border-[#e3e7f1] bg-white px-[12px] py-[8px]">
        <div className="mb-[6px] text-[11px] font-medium text-[#7b849c]">
          会话产物（{items.length}）
        </div>
        <div className="flex flex-wrap gap-[6px]">
          {items.map((artifact) => (
            <button
              key={artifact.id}
              type="button"
              title={`${artifact.kind} · ${formatBytes(artifact.size_bytes)}`}
              onClick={() => {
                void downloadConversationArtifact(artifact).catch((error: unknown) => {
                  notify.error(error instanceof Error ? error.message : '下载失败');
                });
              }}
              className="flex items-center gap-[6px] rounded-[8px] border border-[#dfe4ef] bg-[#f8fafd] px-[9px] py-[5px] text-[12px] text-[#2b5cb8] hover:bg-[#f1f6ff]"
            >
              <span className="max-w-[220px] truncate">{artifact.name}</span>
              <span className="text-[10px] text-[#9aa3b8]">v{artifact.version}</span>
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}
