import { api, apiDownload } from './client';

export type ConversationArtifactView = {
  id: string;
  name: string;
  kind: string;
  mime: string;
  size_bytes: number;
  version: number;
  created_at: string;
  download_url: string;
  task_id?: string;
};

export function listConversationArtifacts(
  sessionId: number,
): Promise<{ items: ConversationArtifactView[] }> {
  return api.get<{ items: ConversationArtifactView[] }>(
    `/api/v1/conversations/${sessionId}/artifacts`,
  );
}

export function downloadConversationArtifact(
  artifact: ConversationArtifactView,
): Promise<void> {
  const filename = artifact.name.split('/').pop() || artifact.name;
  return apiDownload.blob(artifact.download_url, filename);
}
