import type { UserInfo } from './api/types';

const AUTH_STORAGE_KEY = 'hdb_auth_session';

export interface AuthSession {
  token: string;
  user: UserInfo;
}

export function getAuthSession(): AuthSession | null {
  const raw = window.localStorage.getItem(AUTH_STORAGE_KEY);
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
