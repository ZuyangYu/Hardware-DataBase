import { getAuthSession } from '../auth';

const resolveApiBase = () => {
  if (import.meta.env.VITE_API_BASE_URL) {
    return import.meta.env.VITE_API_BASE_URL;
  }
  // dev 下走 vite proxy(/api -> HDB_API_PORT/VITE_API_PROXY_TARGET);生产部署由静态服务器反代
  return '';
};

const API_BASE = resolveApiBase();

export class ApiError extends Error {
  status: number;
  body: string;

  constructor(status: number, body: string, statusText: string) {
    super(parseErrorMessage(body) || statusText || `HTTP ${status}`);
    this.name = 'ApiError';
    this.status = status;
    this.body = body;
  }
}

export function isAuthError(error: unknown): boolean {
  return error instanceof ApiError && error.status === 401;
}

export function isForbiddenError(error: unknown): boolean {
  return error instanceof ApiError && error.status === 403;
}

type UnauthorizedHandler = () => void;

let unauthorizedHandler: UnauthorizedHandler | null = null;

/** 注册全局 401 处理器(token 失效时统一回登录页);传 null 取消注册。 */
export function onUnauthorized(handler: UnauthorizedHandler | null): void {
  unauthorizedHandler = handler;
}

function parseErrorMessage(body: string): string {
  if (!body) return '';
  try {
    const parsed = JSON.parse(body) as { detail?: unknown };
    if (typeof parsed.detail === 'string') return parsed.detail;
    if (parsed.detail && typeof parsed.detail === 'object') {
      const detail = parsed.detail as { message?: unknown; warnings?: unknown };
      const message = typeof detail.message === 'string' ? detail.message : '';
      const warnings = Array.isArray(detail.warnings)
        ? detail.warnings.filter((item): item is string => typeof item === 'string')
        : [];
      return [message, ...warnings].filter(Boolean).join('；');
    }
    if (Array.isArray(parsed.detail)) {
      // FastAPI 422: detail 是 {loc,msg} 列表
      return parsed.detail
        .map((item) => (item && typeof item === 'object' && 'msg' in item ? String((item as { msg: unknown }).msg) : ''))
        .filter(Boolean)
        .join('; ');
    }
    return '';
  } catch {
    return '';
  }
}

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  // 记录本次请求是否携带了本地 token:登录等无凭据请求的 401(如密码错误)不应触发全局登出
  const hasCredentials = Boolean(getAuthSession()?.token);
  const response = await fetch(`${API_BASE}${path}`, {
    headers: {
      'Content-Type': 'application/json',
      ...authHeader(),
      ...(options.headers || {}),
    },
    ...options,
  });
  if (!response.ok) {
    const text = await response.text();
    if (response.status === 401 && hasCredentials) unauthorizedHandler?.();
    throw new ApiError(response.status, text, response.statusText);
  }
  return response.json() as Promise<T>;
}

function authHeader(): Record<string, string> {
  const session = getAuthSession();
  return session?.token ? { Authorization: `Bearer ${session.token}` } : {};
}

export const api = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: 'POST', body: body === undefined ? undefined : JSON.stringify(body) }),
  put: <T>(path: string, body: unknown) => request<T>(path, { method: 'PUT', body: JSON.stringify(body) }),
  patch: <T>(path: string, body: unknown) => request<T>(path, { method: 'PATCH', body: JSON.stringify(body) }),
  delete: <T>(path: string, body?: unknown) => request<T>(path, {
    method: 'DELETE',
    body: body === undefined ? undefined : JSON.stringify(body),
  }),
};

/** 触发浏览器下载二进制文件(不能走 JSON request()). */
export async function downloadBlob(path: string, filename: string): Promise<void> {
  const response = await fetch(`${API_BASE}${path}`, {
    headers: { ...authHeader() },
  });
  if (!response.ok) {
    const text = await response.text();
    throw new ApiError(response.status, text, response.statusText);
  }
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  // Let the browser start the navigation before releasing the object URL;
  // immediate revocation intermittently produces a zero-byte download.
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export const apiDownload = { blob: downloadBlob };

/** multipart 上传(不能走 request():浏览器要自己拼 boundary) */
export async function uploadFiles<T>(path: string, form: FormData): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    method: 'POST',
    headers: { ...authHeader() },
    body: form,
  });
  if (!response.ok) {
    const text = await response.text();
    throw new ApiError(response.status, text, response.statusText);
  }
  return response.json() as Promise<T>;
}

/** 带上传进度回调的 multipart 上传(XHR 才有 upload.onprogress)。 */
export function uploadFilesWithProgress<T>(
  path: string,
  form: FormData,
  onProgress?: (percent: number) => void,
): Promise<T> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', `${API_BASE}${path}`);
    for (const [key, value] of Object.entries(authHeader())) {
      xhr.setRequestHeader(key, value);
    }
    xhr.upload.addEventListener('progress', (event) => {
      if (event.lengthComputable && onProgress) {
        onProgress(Math.round((event.loaded / event.total) * 100));
      }
    });
    xhr.addEventListener('load', () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        try {
          resolve(JSON.parse(xhr.responseText) as T);
        } catch {
          resolve(xhr.responseText as unknown as T);
        }
      } else {
        let detail = xhr.responseText;
        try {
          detail = JSON.parse(xhr.responseText)?.detail ?? xhr.responseText;
        } catch { /* 保持原文 */ }
        reject(new ApiError(xhr.status, detail, xhr.statusText));
      }
    });
    xhr.addEventListener('error', () => reject(new Error('网络错误，上传失败')));
    xhr.send(form);
  });
}

/** SSE 流式请求(POST /query):fetch + ReadableStream 自解析 event/data 帧 */
export type SseEvent = { event: string; data: string; id?: number };

async function* parseSseResponse(response: Response): AsyncGenerator<SseEvent, void, unknown> {
  if (!response.ok) {
    const text = await response.text();
    throw new ApiError(response.status, text, response.statusText);
  }
  if (!response.body) {
    throw new Error('SSE 响应没有 body');
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder('utf-8');
  let buffer = '';
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let sepIndex: number;
      while ((sepIndex = buffer.indexOf('\n\n')) >= 0) {
        const frame = buffer.slice(0, sepIndex);
        buffer = buffer.slice(sepIndex + 2);
        let event = 'message';
        let id: number | undefined;
        const dataLines: string[] = [];
        for (const line of frame.split('\n')) {
          if (line.startsWith('event:')) event = line.slice(6).trim();
          else if (line.startsWith('id:')) {
            const parsed = Number(line.slice(3).trim());
            if (Number.isFinite(parsed)) id = parsed;
          }
          else if (line.startsWith('data:')) dataLines.push(line.startsWith('data: ') ? line.slice(6) : line.slice(5));
        }
        if (dataLines.length > 0) yield { event, data: dataLines.join('\n'), ...(id === undefined ? {} : { id }) };
      }
    }
  } finally {
    reader.cancel().catch(() => undefined);
  }
}

export async function* sseStream(
  path: string,
  body: unknown,
  signal?: AbortSignal,
): AsyncGenerator<SseEvent, void, unknown> {
  const response = await fetch(`${API_BASE}${path}`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Accept: 'text/event-stream',
      ...authHeader(),
    },
    body: JSON.stringify(body),
    signal,
  });
  yield* parseSseResponse(response);
}

/** 可重放的 turn SSE 订阅; 后端会根据 Last-Event-ID 补发持久化事件。 */
export async function* sseGetStream(path: string, signal?: AbortSignal, lastEventId?: number): AsyncGenerator<SseEvent, void, unknown> {
  let cursor = lastEventId || 0;
  let retryAttempt = 0;
  const retryDelaysMs = [500, 1000, 2000, 4000, 8000, 12000, 20000];

  const isAbortError = (error: unknown) =>
    (error instanceof DOMException && error.name === 'AbortError') || signal?.aborted === true;

  const waitForRetry = (delayMs: number): Promise<void> => {
    if (!signal) return new Promise((resolve) => setTimeout(resolve, delayMs));
    if (signal.aborted) return Promise.reject(new DOMException('SSE stream aborted', 'AbortError'));
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        signal.removeEventListener('abort', abort);
        resolve();
      }, delayMs);
      const abort = () => {
        clearTimeout(timer);
        signal.removeEventListener('abort', abort);
        reject(new DOMException('SSE stream aborted', 'AbortError'));
      };
      signal.addEventListener('abort', abort, { once: true });
    });
  };

  for (;;) {
    try {
      const response = await fetch(`${API_BASE}${path}`, {
        headers: {
          Accept: 'text/event-stream',
          ...authHeader(),
          ...(cursor ? { 'Last-Event-ID': String(cursor) } : {}),
        },
        signal,
      });
      for await (const evt of parseSseResponse(response)) {
        if (evt.id !== undefined) cursor = evt.id;
        retryAttempt = 0;
        yield evt;
        // A terminal event ends the durable subscription. If the connection
        // drops before it arrives, the loop below reconnects from `cursor`.
        if (evt.event === 'done' || evt.event === 'error') return;
      }
    } catch (error) {
      if (isAbortError(error)) throw error;
      // Authentication, authorization, and missing-turn errors will not be
      // fixed by retrying. Network errors and 5xx responses are retryable.
      if (error instanceof ApiError && error.status >= 400 && error.status < 500 && error.status !== 408 && error.status !== 429) {
        throw error;
      }
    }

    if (signal?.aborted) throw new DOMException('SSE stream aborted', 'AbortError');
    if (retryAttempt >= retryDelaysMs.length) {
      throw new Error('流式连接中断，自动重连失败，请刷新页面继续接收结果');
    }
    await waitForRetry(retryDelaysMs[retryAttempt]);
    retryAttempt += 1;
  }
}
