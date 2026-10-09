import { useEffect, useState } from 'react';
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
import {
  Sheet,
  SheetContent,
  SheetTitle,
} from '@/components/ui/sheet';
import { cn } from '@/lib/utils';
import AccountSettingsDialog from '@/components/AccountSettingsDialog';
import AppIcon from '@/components/AppIcon';
import BrandLogo from './BrandLogo';
import { useIsMobile } from '@/hooks/use-mobile';
import { hasDashboardAccess, isSystemAdmin, ROLE_LABELS, type AuthSession } from '../auth';

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
  const { toggleSidebar, state, isMobile, openMobile, setOpenMobile } = useSidebar();
  const [settingsOpen, setSettingsOpen] = useState(false);
  const sysAdmin = isSystemAdmin(auth.user);
  const anyAdmin = hasDashboardAccess(auth.user);
  const initial = auth.user.username.slice(0, 1).toUpperCase();
  const collapsed = !isMobile && state === 'collapsed';
  /** 抽屉内导航:跳转后自动收起,避免遮挡目标页面。 */
  const navigateAndClose = (to: string) => {
    navigate(to);
    if (isMobile) setOpenMobile(false);
  };
  /* 移动抽屉的 Escape 关闭。Radix 的 onEscapeKeyDown 链在本项目 radix-ui
     统一包的版本上未稳定触发,这里在 document capture 阶段自行兜底:
     先 preventDefault 再关闭,避免双重处理冲突。 */
  useEffect(() => {
    if (!isMobile || !openMobile) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      event.preventDefault();
      setOpenMobile(false);
    };
    document.addEventListener('keydown', onKeyDown, { capture: true });
    return () => document.removeEventListener('keydown', onKeyDown, { capture: true });
  }, [isMobile, openMobile]);

  const navBody = (
    <div className="flex h-full w-full shrink-0 flex-col">
      {/* 头部:品牌 + 收起/展开按钮。收起态点 mark 展开回边栏。抽屉自带头部,不重复渲染。 */}
      {!isMobile && (
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
        )}

        {/* 导航:铺平,无分组卡片。内容管理是 KB 下钻,不作为侧边栏常驻模块。 */}
        <SidebarContent className={cn(collapsed ? 'items-center justify-center px-0 py-[14px]' : 'px-[16px] pt-[14px]')}>
          {!sysAdmin && (
            <SidebarMenu className={cn(collapsed ? 'items-center gap-[12px]' : 'gap-[8px]')}>
              <PrimaryNavButton
                label="资产中心"
                iconName="grid"
                active={location.pathname === '/assets'}
                collapsed={collapsed}
                onClick={() => navigateAndClose('/assets')}
                tooltip="资产中心"
              />
              <PrimaryNavButton
                label="全部知识库"
                iconName="database"
                active={location.pathname === '/kbs' || location.pathname.startsWith('/kbs/')}
                collapsed={collapsed}
                onClick={() => navigateAndClose('/kbs')}
                tooltip="全部知识库"
              />
              <PrimaryNavButton
                label="对话"
                iconName="send"
                active={location.pathname === '/chat'}
                collapsed={collapsed}
                onClick={() => navigateAndClose('/chat')}
                tooltip="对话"
              />
              <PrimaryNavButton
                label="长期记忆"
                iconName="history"
                active={location.pathname === '/memory'}
                collapsed={collapsed}
                onClick={() => navigateAndClose('/memory')}
                tooltip="长期记忆"
              />
              <PrimaryNavButton
                label="扩展"
                iconName="tool"
                active={location.pathname === '/extensions'}
                collapsed={collapsed}
                onClick={() => navigateAndClose('/extensions')}
                tooltip="外部扩展"
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
                {sysAdmin && (
                  <PrimaryNavButton
                    label="员工管理"
                    iconName="user"
                    active={location.pathname === '/admin/users'}
                    collapsed={collapsed}
                    onClick={() => navigateAndClose('/admin/users')}
                    tooltip="员工管理"
                  />
                )}
                {sysAdmin && (
                  <PrimaryNavButton
                    label="部门管理"
                    iconName="folder"
                    active={location.pathname === '/admin/departments'}
                    collapsed={collapsed}
                    onClick={() => navigateAndClose('/admin/departments')}
                    tooltip="部门管理"
                  />
                )}
                {sysAdmin && (
                  <PrimaryNavButton
                    label="知识库挂载"
                    iconName="lock"
                    active={location.pathname === '/admin/kb-mount'}
                    collapsed={collapsed}
                    onClick={() => navigateAndClose('/admin/kb-mount')}
                    tooltip="知识库挂载"
                  />
                )}
                <PrimaryNavButton
                  label="治理面板"
                  iconName="grid"
                  active={location.pathname === '/admin/governance'}
                  collapsed={collapsed}
                  onClick={() => navigateAndClose('/admin/governance')}
                  tooltip="知识库治理"
                />
                <PrimaryNavButton
                  label="日志中心"
                  iconName="history"
                  active={location.pathname === '/admin/logs'}
                  collapsed={collapsed}
                  onClick={() => navigateAndClose('/admin/logs')}
                  tooltip="日志中心"
                />
                <PrimaryNavButton
                  label="质量工作台"
                  iconName="grid"
                  active={location.pathname === '/admin/quality'}
                  collapsed={collapsed}
                  onClick={() => navigateAndClose('/admin/quality')}
                  tooltip="生产反馈与案例复核"
                />
                <PrimaryNavButton label="成本与用量" iconName="grid" active={location.pathname === '/admin/costs'} collapsed={collapsed} onClick={() => navigateAndClose('/admin/costs')} tooltip="调用费用与覆盖率" />
                {sysAdmin && (
                  <PrimaryNavButton
                    label="系统状态"
                    iconName="refresh"
                    active={location.pathname === '/admin/status'}
                    collapsed={collapsed}
                    onClick={() => navigateAndClose('/admin/status')}
                    tooltip="系统状态"
                  />
                )}
                {sysAdmin && (
                  <PrimaryNavButton
                    label="扩展插件"
                    iconName="tool"
                    active={location.pathname === '/admin/plugins'}
                    collapsed={collapsed}
                    onClick={() => navigateAndClose('/admin/plugins')}
                    tooltip="外部 MCP 扩展"
                  />
                )}
                {sysAdmin && (
                  <PrimaryNavButton
                    label="系统配置"
                    iconName="tool"
                    active={location.pathname === '/admin/config'}
                    collapsed={collapsed}
                    onClick={() => navigateAndClose('/admin/config')}
                    tooltip="系统配置"
                  />
                )}
                {sysAdmin && (
                  <PrimaryNavButton
                    label="RAGAS 评估"
                    iconName="grid"
                    active={location.pathname === '/admin/evaluation'}
                    collapsed={collapsed}
                    onClick={() => navigateAndClose('/admin/evaluation')}
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
              onClick={() => setSettingsOpen(true)}
              title="个人设置"
              aria-label="个人设置"
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
                  onSelect={() => setSettingsOpen(true)}
                  className="h-[36px] cursor-pointer gap-2 rounded-[10px] px-[12px] text-[14px] text-[#464C5E]"
                >
                  <AppIcon name="user" size={16} />
                  个人设置
                </DropdownMenuItem>
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
  );

  return (
    <>
      <AccountSettingsDialog
        open={settingsOpen}
        onOpenChange={setSettingsOpen}
        auth={auth}
        onLogout={onLogout}
      />

      {isMobile ? (
        /* 移动端:侧栏收纳为左抽屉。遮罩、Escape 关闭、焦点返回由 Radix Dialog 提供;
           打开时默认锁定背景滚动。trigger 在 AppHeader(共享页头)内。 */
        <Sheet open={openMobile} onOpenChange={setOpenMobile}>
          <SheetContent
            side="left"
            className="w-[280px] max-w-[85vw] gap-0 p-0"
            onEscapeKeyDown={() => setOpenMobile(false)}
            onCloseAutoFocus={(event) => {
              // 程序化打开(trigger 在 AppHeader,非 SheetTrigger 后代)时 Radix 无从
              // 返回焦点,手动聚焦导航按钮,保持键盘路径连续。
              const trigger = document.getElementById('admin-nav-trigger');
              if (trigger) { event.preventDefault(); trigger.focus(); }
            }}
          >
            <div className="flex h-full min-h-0 flex-col">
              <div className="flex items-center gap-2 border-b border-sidebar-border px-4 py-3">
                <BrandLogo />
                <SheetTitle className="text-sm font-semibold">导航</SheetTitle>
              </div>
              <div className="min-h-0 flex-1 overflow-y-auto">{navBody}</div>
            </div>
          </SheetContent>
        </Sheet>
      ) : (
        <Sidebar collapsible="icon" className={SIDEBAR_SHELL_CLASS}>{navBody}</Sidebar>
      )}
    </>
  );
}
