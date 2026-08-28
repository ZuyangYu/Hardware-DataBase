import type { UserInfo } from './api/types';

const AUTH_STORAGE_KEY = 'hdb_auth_session';

export interface AuthSession {
  token: string;
  user: UserInfo;
}

export function getAuthSession(): AuthSession | null {
  let raw: string | null = null;
  try {
    raw = window.localStorage.getItem(AUTH_STORAGE_KEY);
  } catch {
    return null;
  }
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw) as AuthSession;
    if (!parsed?.token || !parsed?.user?.username) return null;
    return parsed;
  } catch {
    return null;
  }
}

export function setAuthSession(session: AuthSession): void {
  window.localStorage.setItem(AUTH_STORAGE_KEY, JSON.stringify(session));
}

export function clearAuthSession(): void {
  window.localStorage.removeItem(AUTH_STORAGE_KEY);
}

/** 订阅其他 tab 的登出(storage 事件只在本 tab 之外触发);返回取消订阅函数。 */
export function subscribeAuthCrossTab(cb: () => void): () => void {
  const listener = (event: StorageEvent) => {
    if (event.key !== AUTH_STORAGE_KEY) return;
    // 比对新旧值:仅当另一 tab 把会话从有值清成 null(登出)时回调;另一 tab 登录不影响本 tab
    if (event.oldValue !== null && event.newValue === null) cb();
  };
  window.addEventListener('storage', listener);
  return () => window.removeEventListener('storage', listener);
}

export function isSystemAdmin(user: UserInfo): boolean {
  return user.role === 'system_admin';
}

export function isDeptAdmin(user: UserInfo): boolean {
  return user.role === 'dept_admin';
}

export function isAnyAdmin(user: UserInfo): boolean {
  return user.role === 'system_admin' || user.role === 'dept_admin';
}

export const ROLE_LABELS: Record<UserInfo['role'], string> = {
  system_admin: '系统管理员',
  dept_admin: '部门管理员',
  user: '普通用户',
};
