import type { UserInfo } from '@/api/types';
import { hasDashboardAccess, isSystemAdmin } from '@/auth';
import type { AppIconName } from '@/components/AppIcon';

type Access = 'workspace' | 'dashboard' | 'system_admin';

export type NavItem = {
  path: string;
  label: string;
  icon: AppIconName;
  description: string;
  access: Access;
  matchChildren?: boolean;
};

export type NavGroup = {
  id: 'workspace' | 'organization' | 'governance' | 'platform';
  label: string;
  items: NavItem[];
};

export type PrimaryNavItem = {
  id: 'workbench' | 'chat' | 'extensions' | 'governance' | 'management';
  path: string;
  label: string;
  icon: AppIconName;
  description: string;
  items: NavItem[];
};

const NAV_GROUPS: NavGroup[] = [
  {
    id: 'workspace', label: '工作区', items: [
      { path: '/assets', label: '资产中心', icon: 'grid', description: '文档与业务资产', access: 'workspace' },
      { path: '/kbs', label: '知识库', icon: 'database', description: '授权知识库', access: 'workspace', matchChildren: true },
      { path: '/chat', label: '对话', icon: 'send', description: '问答与对话', access: 'workspace' },
      { path: '/tasks', label: '任务', icon: 'tool', description: '通用 agent 任务与产物', access: 'workspace' },
      { path: '/memory', label: '长期记忆', icon: 'history', description: '个人与项目记忆', access: 'workspace' },
      { path: '/extensions', label: '个人扩展', icon: 'tool', description: '订阅和管理你的 Skill 与 MCP', access: 'workspace' },
    ],
  },
  {
    id: 'organization', label: '组织与权限', items: [
      { path: '/admin/departments', label: '部门管理', icon: 'folder', description: '部门结构与归属', access: 'system_admin' },
      { path: '/admin/users', label: '员工管理', icon: 'user', description: '员工账号与启停', access: 'system_admin' },
      { path: '/admin/kb-mount', label: '知识库分配', icon: 'lock', description: '知识库部门归属与访问授权', access: 'system_admin' },
    ],
  },
  {
    id: 'governance', label: '治理与运营', items: [
      { path: '/admin/governance', label: '治理总览', icon: 'grid', description: '知识库治理与运营入口', access: 'dashboard' },
      { path: '/admin/quality', label: '反馈工单', icon: 'check-list', description: '员工反馈与问题处理', access: 'dashboard', matchChildren: true },
      { path: '/admin/costs', label: '成本与用量', icon: 'chart', description: '模型调用费用与覆盖率', access: 'dashboard' },
      { path: '/admin/logs', label: '日志中心', icon: 'logs', description: '审计记录与查询日志', access: 'dashboard' },
      { path: '/admin/evaluation', label: 'RAGAS 评估', icon: 'flask', description: '评估运行与质量门禁', access: 'system_admin' },
    ],
  },
  {
    id: 'platform', label: '平台管理', items: [
      { path: '/admin/status', label: '系统状态', icon: 'activity', description: '服务状态与运行诊断', access: 'system_admin' },
      { path: '/admin/plugins', label: '平台扩展', icon: 'tool', description: '登记、审核和配置外部能力', access: 'system_admin' },
      { path: '/admin/config', label: '系统配置', icon: 'settings', description: '平台参数与模型配置', access: 'system_admin' },
    ],
  },
];

const workspaceGroup = () => NAV_GROUPS.find((group) => group.id === 'workspace')?.items ?? [];
const governanceGroup = () => NAV_GROUPS.find((group) => group.id === 'governance')?.items ?? [];
const organizationGroup = () => NAV_GROUPS.find((group) => group.id === 'organization')?.items ?? [];
const platformGroup = () => NAV_GROUPS.find((group) => group.id === 'platform')?.items ?? [];

/**
 * 左侧只展示一级工作区;具体页面通过 AppHeader 下的二级导航访问。
 * 这样不会牺牲现有 URL,也不会把权限判断下沉到视觉层。
 */
const PRIMARY_NAV_ITEMS: (Omit<PrimaryNavItem, 'items'> & { itemSource: () => NavItem[] })[] = [
  {
    id: 'workbench',
    path: '/assets',
    label: '工作台',
    icon: 'grid',
    description: '知识库与业务资产',
    itemSource: () => workspaceGroup().filter((item) => item.path === '/assets' || item.path === '/kbs' || item.path === '/tasks'),
  },
  {
    id: 'chat',
    path: '/chat',
    label: '对话',
    icon: 'send',
    description: '问答与对话',
    itemSource: () => workspaceGroup().filter((item) => item.path === '/chat'),
  },
  {
    id: 'extensions',
    path: '/extensions',
    label: '个人扩展',
    icon: 'tool',
    description: '订阅和管理你的 Skill 与 MCP',
    itemSource: () => workspaceGroup().filter((item) => item.path === '/extensions'),
  },
  {
    id: 'governance',
    path: '/admin/governance',
    label: '治理与运营',
    icon: 'activity',
    description: '质量、成本与运行治理',
    itemSource: governanceGroup,
  },
  {
    id: 'management',
    path: '/admin/departments',
    label: '管理',
    icon: 'settings',
    description: '组织与平台管理',
    itemSource: () => [...organizationGroup(), ...platformGroup()],
  },
];

/** 只用于可发现性；不替代 AdminRoute 或后端授权。保留原有角色可见性。 */
export function navigationGroups(user: UserInfo): NavGroup[] {
  return NAV_GROUPS.map((group) => ({
    ...group,
    items: group.items.filter((item) => item.access === 'workspace' ? !isSystemAdmin(user)
      : item.access === 'system_admin' ? isSystemAdmin(user) : hasDashboardAccess(user)).map(item => item.path === '/admin/quality' && !isSystemAdmin(user) ? { ...item, label: '我的反馈' } : item),
  })).filter((group) => group.items.length > 0);
}

function canSeeNavItem(item: NavItem, user: UserInfo): boolean {
  return item.access === 'workspace' ? !isSystemAdmin(user)
    : item.access === 'system_admin' ? isSystemAdmin(user) : hasDashboardAccess(user);
}

export function primaryNavigationItems(user: UserInfo): PrimaryNavItem[] {
  return PRIMARY_NAV_ITEMS.map((item) => ({
    ...item,
    items: item.itemSource().filter((child) => canSeeNavItem(child, user)).map(child => child.path === '/admin/quality' && !isSystemAdmin(user) ? { ...child, label: '我的反馈' } : child),
  })).filter((item) => item.items.length > 0);
}

export function isNavItemActive(item: NavItem, location: string): boolean {
  const pathname = location.split(/[?#]/, 1)[0].replace(/\/$/, '');
  return pathname === item.path || Boolean(item.matchChildren && pathname.startsWith(`${item.path}/`));
}

export function isPrimaryNavItemActive(item: PrimaryNavItem, location: string): boolean {
  return location.split(/[?#]/, 1)[0].replace(/\/$/, '') === item.path
    || item.items.some((child) => isNavItemActive(child, location));
}

export function activePrimaryNavigationItem(user: UserInfo, location: string): PrimaryNavItem | undefined {
  return primaryNavigationItems(user).find((item) => isPrimaryNavItemActive(item, location));
}

export function activeNavigationGroup(user: UserInfo, location: string): NavGroup | undefined {
  return navigationGroups(user).find((group) => group.items.some((item) => isNavItemActive(item, location)));
}
