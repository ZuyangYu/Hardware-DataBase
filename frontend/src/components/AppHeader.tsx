import { useContext, type ReactNode } from 'react';
import { Link, useLocation } from 'react-router-dom';

import { Menu } from 'lucide-react';

import { cn } from '@/lib/utils';
import { SidebarContext } from '@/components/ui/sidebar';
import { Button } from '@/components/ui/button';

import NotificationBell from './NotificationBell';
import { NavigationSectionContext } from './NavigationSectionContext';
import { isNavItemActive } from '@/lib/navigation';

export type AppHeaderProps = {
  /**
   * 页面专属内容,渲染在 header 左侧。提供时优先于 title/description。
   */
  left?: ReactNode;
  /** 左侧标题行(left 未设时用)。 */
  title?: ReactNode;
  /** 左侧描述行(left 未设时用)。 */
  description?: ReactNode;
  /** 管理端分组上下文，默认由 Shell 的当前路由提供。 */
  sectionLabel?: string;
  /**
   * 右侧自定义内容。提供时完全替换默认的通知铃铛
   * (如登录页放主题切换 + 登录按钮)。
   */
  right?: ReactNode;
  className?: string;
};

/**
 * 全局页头。右侧默认只放通知铃铛:用户头像统一收敛到左侧边栏底部
 * (AppSidebar),同一入口不再出现两处头像。左侧由各页通过 left 槽
 * 或 title/description 提供。照搬 AppHeader 结构(svgr 图标换成 lucide)。
 */
export default function AppHeader({
  left,
  title,
  description,
  sectionLabel,
  right,
  className,
}: AppHeaderProps) {
  const currentSection = useContext(NavigationSectionContext);
  const location = useLocation();
  const section = sectionLabel ?? currentSection?.sectionLabel;
  const subnav = currentSection?.subnav ?? [];
  const leftContent = left ?? (
    (title !== undefined || description !== undefined) ? (
      <div className="flex min-h-[44px] flex-col justify-center gap-[3px]">
        {section && <p className="text-[11px] font-semibold tracking-[0.04em] text-[#596174] md:text-[12px]" aria-label="当前导航分组">{section}</p>}
        {title !== undefined && (
          <h1 className="text-[18px] font-semibold leading-[1.25] tracking-[-0.01em] text-[#24324a] md:text-[20px]">{title}</h1>
        )}
        {description !== undefined && (
          <p className="text-[13px] leading-[1.5] text-[#596174] md:text-[14px]">{description}</p>
        )}
      </div>
    ) : null
  );

  // 移动端导航入口:窄屏(<768px)管理侧栏收纳为抽屉,由本按钮打开。
  // context 为 null 时(登录页等无侧栏场景)不渲染,不影响 provider 外使用。
  const sidebar = useContext(SidebarContext);
  const showNavTrigger = sidebar?.isMobile === true;

  return (
    <header className={cn('grid w-full gap-[14px]', className)}>
      <div className="flex w-full items-start gap-[16px]">
        {showNavTrigger && (
          <Button
            type="button"
            variant="ghost"
            size="icon"
            id="admin-nav-trigger"
            aria-label="打开导航"
            title="打开导航"
            className="mt-0.5 size-9 shrink-0 md:hidden"
            onClick={() => sidebar.setOpenMobile(true)}
          >
            <Menu className="size-5" aria-hidden />
          </Button>
        )}
        <div className="min-w-0 flex-1">{leftContent}</div>
        <div className="flex h-[32px] shrink-0 items-center gap-[8px]">
          {right !== undefined ? right : <NotificationBell />}
        </div>
      </div>
      {subnav.length > 1 && (
        <nav aria-label="页面二级导航" className="min-w-0 overflow-x-auto rounded-[13px] border border-[#e7ebf2] bg-[#f8fafc] p-[4px]">
          <div className="flex min-w-max items-center gap-[3px]">
            {subnav.map((item) => {
              const active = isNavItemActive(item, location.pathname);
              return (
                <Link
                  key={item.path}
                  to={item.path}
                  aria-current={active ? 'page' : undefined}
                  className={cn(
                    'relative inline-flex min-h-[32px] items-center whitespace-nowrap rounded-[9px] px-[11px] text-[13px] text-[#596174] transition-colors hover:bg-white/70 hover:text-[#24324a] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#8797c5] focus-visible:ring-offset-1 md:min-h-[36px] md:px-[13px] md:text-[14px]',
                    active && 'bg-white font-semibold text-[#24324a] shadow-[0_1px_3px_rgba(36,50,74,0.10)]',
                  )}
                >
                  {item.label}
                </Link>
              );
            })}
          </div>
        </nav>
      )}
    </header>
  );
}
