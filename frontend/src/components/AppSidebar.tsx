import { useEffect, useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { BookOpen } from 'lucide-react';

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
import { isEmployee, ROLE_LABELS, type AuthSession } from '../auth';
import { isPrimaryNavItemActive, primaryNavigationItems } from '@/lib/navigation';

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
  className,
}: {
  label: string;
  iconName: Parameters<typeof AppIcon>[0]['name'];
  active: boolean;
  collapsed: boolean;
  disabled?: boolean;
  onClick: () => void;
  tooltip?: string;
  className?: string;
}) {
  return (
    <SidebarMenuItem className={cn('relative', collapsed ? 'flex w-full justify-center' : 'w-full', className)}>
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
        aria-current={active ? 'page' : undefined}
        disabled={disabled}
        onClick={onClick}
        className={cn(
          'text-[14px] text-sidebar-foreground md:text-[15px]',
          collapsed
            ? 'mx-auto flex h-[46px]! w-[46px]! flex-none items-center justify-center rounded-[13px]! p-0!'
            : 'h-[46px] gap-[12px] rounded-[14px] px-[16px] py-[10px]',
          'hover:bg-sidebar-accent hover:text-sidebar-accent-foreground',
          active && 'bg-[#eceff4] text-[#18181a] shadow-[inset_0_0_0_1px_#e1e5ee]',
          'data-[active=true]:bg-[#eceff4] data-[active=true]:text-[#18181a] data-[active=true]:font-semibold',
        )}
      >
        <AppIcon name={iconName} size={collapsed ? 21 : 20} className={cn('shrink-0', active && 'text-[#18181a]')} />
        {!collapsed && (
          <span className={cn('text-[14px] md:text-[15px]', active && 'font-semibold text-[#18181a]')}>{label}</span>
        )}
      </SidebarMenuButton>
    </SidebarMenuItem>
  );
}

export default function AppSidebar({ auth, onLogout }: Props) {
  const navigate = useNavigate();
  const location = useLocation();
  const { toggleSidebar, state, isMobile, openMobile, setOpenMobile } = useSidebar();
  const [settingsOpen, setSettingsOpen] = useState(false);
  const primaryItems = primaryNavigationItems(auth.user);
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

        {/* 只保留一级工作区;具体页面在 AppHeader 下的二级导航中切换。 */}
        <SidebarContent className={cn(collapsed ? 'items-center justify-center px-0 py-[14px]' : 'px-[16px] pt-[14px]')}>
          <SidebarMenu className={cn(collapsed ? 'items-center gap-[12px]' : 'gap-[8px]')}>
            {primaryItems.map((item, itemIndex) => {
              const active = isPrimaryNavItemActive(item, location.pathname);
              const separated = itemIndex > 0 && (item.id === 'governance' || item.id === 'management');
              return (
                <PrimaryNavButton
                  key={item.id}
                  label={item.label}
                  iconName={item.icon}
                  active={active}
                  collapsed={collapsed}
                  onClick={() => navigateAndClose(item.path)}
                  tooltip={`${item.label}：${item.description}`}
                  className={cn(
                    separated && !collapsed && 'mt-[12px] border-t border-sidebar-border pt-[16px]',
                    separated && collapsed && 'mt-[10px] border-t border-sidebar-border pt-[12px]',
                  )}
                />
              );
            })}
          </SidebarMenu>
        </SidebarContent>

        {/* 底部:用户菜单(保留退出;AppHeader 也有,这里冗余但常见) */}
        <SidebarFooter className="px-[20px] pb-[20px] group-data-[collapsible=icon]:px-[12px]">
          <SidebarMenu className="mb-2">
            <SidebarMenuItem>
              <SidebarMenuButton
                asChild
                tooltip="使用帮助（在新标签页打开）"
                className={cn(
                  'h-[40px] rounded-[12px] text-[14px] text-[#5b6478]',
                  collapsed ? 'mx-auto flex h-[46px]! w-[46px]! justify-center p-0!' : 'gap-[10px] px-[8px]',
                )}
              >
                <a href="/help" target="_blank" rel="noopener noreferrer" aria-label="使用帮助" title="使用帮助（在新标签页打开）">
                  <BookOpen className="size-[20px] shrink-0" aria-hidden="true" />
                  {!collapsed && <span>使用帮助</span>}
                  <span className="sr-only">在新标签页打开</span>
                </a>
              </SidebarMenuButton>
            </SidebarMenuItem>
          </SidebarMenu>
          <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <button
                  type="button"
                  aria-label="打开个人菜单"
                  className={cn(
                    'flex w-full items-center rounded-[14px] py-[8px] text-left transition-colors hover:bg-sidebar-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sidebar-ring',
                    collapsed ? 'justify-center px-0' : 'gap-[10px] px-[8px]',
                  )}
                >
                  <span className="grid size-[32px] shrink-0 place-items-center overflow-hidden rounded-full bg-[#eef1fb] text-[13px] font-bold text-[#7e96dc]">
                    {initial}
                  </span>
                  {!collapsed && <span className="min-w-0 flex-1">
                    <span className="block truncate text-[13px] font-semibold text-foreground md:text-[14px]">
                      {auth.user.username}
                    </span>
                    <span className="block truncate text-[11px] text-muted-foreground md:text-[12px]">
                      {ROLE_LABELS[auth.user.role]}
                      {auth.user.department_name ? ` · ${auth.user.department_name}` : ''}
                    </span>
                  </span>}
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
                {isEmployee(auth.user) && (
                  <DropdownMenuItem
                    onSelect={() => navigateAndClose('/memory')}
                    className="h-[36px] cursor-pointer gap-2 rounded-[10px] px-[12px] text-[14px] text-[#464C5E]"
                  >
                    <AppIcon name="history" size={16} />
                    长期记忆
                  </DropdownMenuItem>
                )}
                <DropdownMenuItem
                  onSelect={onLogout}
                  className="h-[36px] cursor-pointer gap-2 rounded-[10px] px-[12px] text-[14px] text-[#464C5E]"
                >
                  <AppIcon name="logout" size={16} />
                  退出登录
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
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
