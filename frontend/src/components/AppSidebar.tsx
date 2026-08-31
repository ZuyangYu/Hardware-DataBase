import { useLocation, useNavigate } from 'react-router-dom';

import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  useSidebar,
} from '@/components/ui/sidebar';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import { cn } from '@/lib/utils';
import AppIcon from '@/components/AppIcon';
import { isAnyAdmin, isSystemAdmin, ROLE_LABELS, type AuthSession } from '../auth';
import BrandLogo from './BrandLogo';

type Props = {
  auth: AuthSession;
  onLogout: () => void;
};

// Shared shell class -- 照搬 SIDEBAR_SHELL_CLASS(毛玻璃 + 右边框)。
const SIDEBAR_SHELL_CLASS =
  'overflow-hidden border-r border-sidebar-border bg-sidebar backdrop-blur-[9.5px] **:data-[slot=sidebar-inner]:bg-sidebar';

/** 一级导航按钮:展开态偏宽松,收起态做成独立 icon rail。 */
function PrimaryNavButton({
  label,
  iconName,
  active,
  collapsed,
  disabled,
  onClick,
  tooltip,
}: {
  label: string;
  iconName: Parameters<typeof AppIcon>[0]['name'];
  active: boolean;
  collapsed: boolean;
  disabled?: boolean;
  onClick: () => void;
  tooltip?: string;
}) {
  return (
    <SidebarMenuItem className={cn('relative', collapsed ? 'flex w-full justify-center' : 'w-full')}>
      {active && !collapsed && (
        <span
          className={cn(
            'absolute top-1/2 left-[0px] h-[24px] w-[3px] -translate-y-1/2 rounded-full bg-[#18181a]',
          )}
        />
      )}
      {active && collapsed && (
        <span
          className={cn(
            'pointer-events-none absolute left-[6px] top-1/2 h-[22px] w-[3px] -translate-y-1/2 rounded-full bg-[#18181a]',
          )}
        />
      )}
      <SidebarMenuButton
        tooltip={tooltip ?? label}
        isActive={active}
        disabled={disabled}
        onClick={onClick}
        className={cn(
          'text-[14px] text-sidebar-foreground',
          collapsed
            ? 'mx-auto flex h-[46px]! w-[46px]! flex-none items-center justify-center rounded-[13px]! p-0!'
            : 'h-[46px] gap-[12px] rounded-[14px] px-[16px] py-[10px]',
          'hover:bg-sidebar-accent hover:text-sidebar-accent-foreground',
          active && 'bg-[#eceff4] text-[#18181a] shadow-[inset_0_0_0_1px_#e1e5ee]',
          'data-active:bg-[#eceff4] data-active:text-[#18181a] data-active:font-semibold',
        )}
      >
        <AppIcon name={iconName} size={collapsed ? 21 : 20} className={cn('shrink-0', active && 'text-[#18181a]')} />
        {!collapsed && (
          <span className={cn('text-[14px]', active && 'font-semibold text-[#18181a]')}>{label}</span>
        )}
      </SidebarMenuButton>
    </SidebarMenuItem>
  );
}

/** 分组标签:照搬 GroupLabel。 */
function GroupLabel({ children }: { children: string }) {
  return (
    <span className="px-[8px] pt-[8px] pb-[3px] text-[11px] leading-none text-[#464c5e]">
      {children}
    </span>
  );
}

export default function AppSidebar({ auth, onLogout }: Props) {
  const navigate = useNavigate();
  const location = useLocation();
  const { toggleSidebar, state } = useSidebar();
  const sysAdmin = isSystemAdmin(auth.user);
  const anyAdmin = isAnyAdmin(auth.user);
  const initial = auth.user.username.slice(0, 1).toUpperCase();
  const collapsed = state === 'collapsed';

  return (
    <Sidebar collapsible="icon" className={SIDEBAR_SHELL_CLASS}>
      <div className="flex h-full w-full shrink-0 flex-col">
        {/* 头部:品牌 + 收起/展开按钮。收起态点 mark 展开回边栏。 */}
        <SidebarHeader className={cn('pt-[18px]', collapsed ? 'px-[12px] pb-[22px]' : 'px-[20px] pb-[26px]')}>
          <div className={cn('flex items-center', collapsed ? 'justify-center' : 'justify-between')}>
            <button
              type="button"
              onClick={collapsed ? toggleSidebar : undefined}
              title={collapsed ? '展开边栏' : undefined}
              aria-label={collapsed ? '展开边栏' : undefined}
              className={collapsed ? 'flex w-full items-center justify-center outline-none' : 'contents'}
            >
              <BrandLogo markOnly={collapsed} wordmarkClassName={collapsed ? 'hidden' : undefined} />
            </button>
            {!collapsed && (
              <button
                type="button"
                onClick={toggleSidebar}
                title="收起边栏"
                aria-label="收起边栏"
                className="flex size-[28px] shrink-0 items-center justify-center rounded-[8px] text-sidebar-foreground transition-colors hover:bg-sidebar-accent hover:text-sidebar-accent-foreground"
              >
                <AppIcon name="sidebar-close" size={14} />
              </button>
            )}
          </div>
        </SidebarHeader>

        {/* 导航:铺平,无分组卡片。内容管理是 KB 下钻,不作为侧边栏常驻模块。 */}
        <SidebarContent className={cn(collapsed ? 'items-center justify-center px-0 py-[14px]' : 'px-[16px] pt-[14px]')}>
          {!sysAdmin && (
            <SidebarMenu className={cn(collapsed ? 'items-center gap-[12px]' : 'gap-[8px]')}>
              <PrimaryNavButton
                label="资产中心"
                iconName="grid"
                active={location.pathname === '/assets'}
                collapsed={collapsed}
                onClick={() => navigate('/assets')}
                tooltip="资产中心"
              />
              <PrimaryNavButton
                label="全部知识库"
                iconName="database"
                active={location.pathname === '/kbs' || location.pathname.startsWith('/kbs/')}
                collapsed={collapsed}
                onClick={() => navigate('/kbs')}
                tooltip="全部知识库"
              />
              <PrimaryNavButton
                label="对话"
                iconName="send"
                active={location.pathname === '/chat'}
                collapsed={collapsed}
                onClick={() => navigate('/chat')}
                tooltip="对话"
              />
              <PrimaryNavButton
                label="长期记忆"
                iconName="history"
                active={location.pathname === '/memory'}
                collapsed={collapsed}
                onClick={() => navigate('/memory')}
                tooltip="长期记忆"
              />
              <PrimaryNavButton
                label="文档生成"
                iconName="file"
                active={location.pathname === '/document-generation'}
                collapsed={collapsed}
                onClick={() => navigate('/document-generation')}
                tooltip="文档生成"
              />
            </SidebarMenu>
          )}

          {anyAdmin && (
            <>
              <div
                className={cn(
                  'h-px bg-sidebar-border',
                  collapsed ? 'mx-auto my-[20px] w-[30px]' : 'w-full',
                  !collapsed && !sysAdmin ? 'my-[22px]' : !collapsed && 'my-[18px]',
                )}
              />
              <SidebarMenu
                className={cn(
                  collapsed ? 'items-center gap-[12px]' : 'gap-[8px]',
                  !collapsed && !sysAdmin && 'mt-[2px]',
                  !collapsed && sysAdmin && 'mt-[6px]',
                )}
              >
                <PrimaryNavButton
                  label="用户管理"
                  iconName="user"
                  active={location.pathname === '/admin/users'}
                  collapsed={collapsed}
                  onClick={() => navigate('/admin/users')}
                  tooltip="用户管理"
                />
                {sysAdmin && (
                  <PrimaryNavButton
                    label="部门管理"
                    iconName="folder"
                    active={location.pathname === '/admin/departments'}
                    collapsed={collapsed}
                    onClick={() => navigate('/admin/departments')}
                    tooltip="部门管理"
                  />
                )}
                <PrimaryNavButton
                  label="知识库授权"
                  iconName="lock"
                  active={location.pathname === '/admin/kb-permissions'}
                  collapsed={collapsed}
                  onClick={() => navigate('/admin/kb-permissions')}
                  tooltip="知识库授权"
                />
                <PrimaryNavButton
                  label="治理面板"
                  iconName="grid"
                  active={location.pathname === '/admin/governance'}
                  collapsed={collapsed}
                  onClick={() => navigate('/admin/governance')}
                  tooltip="知识库治理"
                />
                <PrimaryNavButton
                  label="日志中心"
                  iconName="history"
                  active={location.pathname === '/admin/logs'}
                  collapsed={collapsed}
                  onClick={() => navigate('/admin/logs')}
                  tooltip="日志中心"
                />
                <PrimaryNavButton
                  label="系统状态"
                  iconName="refresh"
                  active={location.pathname === '/admin/status'}
                  collapsed={collapsed}
                  onClick={() => navigate('/admin/status')}
                  tooltip="系统状态"
                />
                {sysAdmin && (
                  <PrimaryNavButton
                    label="系统配置"
                    iconName="tool"
                    active={location.pathname === '/admin/config'}
                    collapsed={collapsed}
                    onClick={() => navigate('/admin/config')}
                    tooltip="系统配置"
                  />
                )}
                {sysAdmin && (
                  <PrimaryNavButton
                    label="RAGAS 评估"
                    iconName="grid"
                    active={location.pathname === '/admin/evaluation'}
                    collapsed={collapsed}
                    onClick={() => navigate('/admin/evaluation')}
                    tooltip="RAGAS 评估"
                  />
                )}
              </SidebarMenu>
            </>
          )}
        </SidebarContent>

        {/* 底部:用户菜单(保留退出;AppHeader 也有,这里冗余但常见) */}
        <SidebarFooter className="px-[20px] pb-[20px] group-data-[collapsible=icon]:px-[12px]">
          {collapsed ? (
            <button
              type="button"
              onClick={toggleSidebar}
              title="展开边栏"
              aria-label="展开边栏"
              className="flex w-full items-center justify-center rounded-[14px] px-0 py-[8px] text-left transition-colors hover:bg-sidebar-accent"
            >
              <span className="grid size-[32px] shrink-0 place-items-center overflow-hidden rounded-full bg-[#eef1fb] text-[13px] font-bold text-[#7e96dc]">
                {initial}
              </span>
            </button>
          ) : (
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <button
                  type="button"
                  className="flex w-full items-center gap-[10px] rounded-[14px] px-[8px] py-[8px] text-left transition-colors hover:bg-sidebar-accent"
                >
                  <span className="grid size-[32px] shrink-0 place-items-center overflow-hidden rounded-full bg-[#eef1fb] text-[13px] font-bold text-[#7e96dc]">
                    {initial}
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-[13px] font-semibold text-foreground">
                      {auth.user.username}
                    </span>
                    <span className="block truncate text-[11px] text-muted-foreground">
                      {ROLE_LABELS[auth.user.role]}
                      {auth.user.department_name ? ` · ${auth.user.department_name}` : ''}
                    </span>
                  </span>
                </button>
              </DropdownMenuTrigger>
              <DropdownMenuContent side="top" align="start" className="w-[200px] rounded-[14px] border-0 bg-white p-[6px] shadow-[0px_16px_15px_rgba(0,0,0,0.1)] ring-0 [--accent:#F6F6F6] [--accent-foreground:#18181A]">
                <DropdownMenuItem
                  onSelect={onLogout}
                  className="h-[36px] cursor-pointer gap-2 rounded-[10px] px-[12px] text-[14px] text-[#464C5E]"
                >
                  <AppIcon name="logout" size={16} />
                  退出登录
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
          )}
        </SidebarFooter>
      </div>
    </Sidebar>
  );
}
