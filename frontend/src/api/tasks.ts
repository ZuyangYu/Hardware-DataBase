import { api, apiDownload } from './client';

export type TaskTurnView = {
  id: string;
  status: string;
  query: string;
  answer: string;
  error_message: string;
  stopped_reason?: string | null;
  partial_answer?: boolean;
  created_at?: string;
  finished_at?: string | null;
};

export type TaskArtifactView = {
  id: string;
  name: string;
  kind: string;
  mime: string;
  size_bytes: number;
  sha256: string;
  version: number;
  created_at: string;
  download_url: string;
};

export type TaskPlanItem = {
  id?: string;
  title?: string;
  status?: string;
};

export type TaskView = {
  id: string;
  title: string;
  kb_name: string;
  session_id: number;
  definition_id: string;
  status: string;
  query: string;
  plan: TaskPlanItem[];
  error_message: string;
  created_at: string;
  updated_at: string;
  finished_at?: string | null;
  current_turn?: TaskTurnView;
  turns: { turn_id: string; kind: string; created_at: string }[];
  artifacts: TaskArtifactView[];
};

export type TaskListResponse = { items: TaskView[] };

export type TaskDefinitionView = {
  id: string;
  title: string;
  goal: string;
  deliverable: string;
  coverage: string[];
  skill: string;
  applies_when: string[];
};

export function listTasks(): Promise<TaskListResponse> {
  return api.get<TaskListResponse>('/api/v1/tasks');
}

export function listTaskDefinitions(): Promise<{ items: TaskDefinitionView[] }> {
  return api.get<{ items: TaskDefinitionView[] }>('/api/v1/task-definitions');
}

export function createTask(body: {
  query: string;
  kb_name?: string;
  definition_id?: string;
  title?: string;
  source_scope?: string;
}): Promise<{ task: TaskView; turn: { id: string; status: string } }> {
  return api.post('/api/v1/tasks', body);
}

export function getTask(taskId: string): Promise<TaskView> {
  return api.get<TaskView>(`/api/v1/tasks/${encodeURIComponent(taskId)}`);
}

export function continueTask(
  taskId: string,
  query: string,
): Promise<{ task: TaskView; turn: { id: string; status: string } }> {
  return api.post(`/api/v1/tasks/${encodeURIComponent(taskId)}/messages`, { query });
}

export function stopTask(taskId: string): Promise<{ task: TaskView }> {
  return api.post(`/api/v1/tasks/${encodeURIComponent(taskId)}/stop`);
}

export function downloadTaskArtifact(artifact: TaskArtifactView): Promise<void> {
  const filename = artifact.name.split('/').pop() || artifact.name;
  return apiDownload.blob(artifact.download_url, filename);
}
