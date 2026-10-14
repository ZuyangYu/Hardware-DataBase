import { describe, expect, it } from 'vitest';

import type { UserInfo } from '@/api/types';
import { navigationGroups, primaryNavigationItems } from './navigation';

const employee: UserInfo = {
  username: 'employee',
  role: 'employee',
  department_id: 47,
  department_name: 'ADAS',
};

const admin: UserInfo = {
  username: 'admin',
  role: 'system_admin',
  department_id: null,
  department_name: null,
};

describe('navigation information architecture', () => {
  it('promotes personal extensions and keeps memory out of primary navigation', () => {
    const items = primaryNavigationItems(employee);

    expect(items.map((item) => item.label)).toEqual([
      '工作台',
      '对话',
      '个人扩展',
      '治理与运营',
    ]);
    expect(items.find((item) => item.label === '个人扩展')?.path).toBe('/extensions');
    expect(items.some((item) => item.label === '更多')).toBe(false);
    expect(items.some((item) => item.items.some((child) => child.path === '/memory'))).toBe(false);
  });

  it('distinguishes platform extensions for system administrators', () => {
    const management = primaryNavigationItems(admin).find((item) => item.label === '管理');
    const platformExtension = management?.items.find((item) => item.path === '/admin/plugins');

    expect(platformExtension?.label).toBe('平台扩展');
    expect(platformExtension?.description).toContain('登记');
  });

  it('keeps personal and platform extension entries in separate role scopes', () => {
    const employeeGroups = navigationGroups(employee);
    const adminGroups = navigationGroups(admin);

    expect(employeeGroups.flatMap((group) => group.items).some((item) => item.path === '/admin/plugins')).toBe(false);
    expect(adminGroups.flatMap((group) => group.items).some((item) => item.path === '/extensions')).toBe(false);
  });
});
