import { useEffect, useMemo, useState } from 'react';

import {
  listConversationArtifacts,
  type ConversationArtifactView,
} from '@/api/artifacts';

export function useConversationArtifacts(
  sessionId: number | null,
  refreshKey?: string | number,
) {
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

  const byTurnId = useMemo(() => {
    const grouped: Record<string, ConversationArtifactView[]> = {};
    for (const item of items) {
      const key = String(item.turn_id || '');
      if (!key) continue;
      if (!grouped[key]) grouped[key] = [];
      grouped[key].push(item);
    }
    return grouped;
  }, [items]);

  return { items, byTurnId };
}
