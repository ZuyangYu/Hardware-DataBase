import { useContext, type ReactNode } from 'react';

import { Menu } from 'lucide-react';

import { cn } from '@/lib/utils';
import { SidebarContext } from '@/components/ui/sidebar';
import { Button } from '@/components/ui/button';

import NotificationBell from './NotificationBell';

export type AppHeaderProps = {
  /**
   * 页面专属内容,渲染在 header 左侧。提供时优先于 title/description。
   */
  left?: ReactNode;
  /** 左侧标题行(left 未设时用)。 */
  title?: ReactNode;
  /** 左侧描述行(left 未设时用)。 */
  description?: ReactNode;
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
  right,
  className,
}: AppHeaderProps) {
  const leftContent = left ?? (
    (title !== undefined || description !== undefined) ? (
      <div className="flex min-h-[40px] flex-col justify-center gap-[4px]">
        {title !== undefined && (
          <h1 className="text-[16px] font-medium leading-[normal] text-[#464c5e]">{title}</h1>
        )}
        {description !== undefined && (
          <p className="text-[14px] leading-[normal] text-[#757f9c]">{description}</p>
        )}
      </div>
    ) : null
  );

  // 移动端导航入口:窄屏(<768px)管理侧栏收纳为抽屉,由本按钮打开。
  // context 为 null 时(登录页等无侧栏场景)不渲染,不影响 provider 外使用。
  const sidebar = useContext(SidebarContext);
  const showNavTrigger = sidebar?.isMobile === true;

  return (
    <header className={cn('flex w-full items-start gap-[16px]', className)}>
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
    </header>
  );
}
