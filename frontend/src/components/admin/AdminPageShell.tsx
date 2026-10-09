import type { ReactNode } from 'react';

import { cn } from '@/lib/utils';

/**
 * 管理页面统一骨架:统一内边距、最小宽度和纵向间距。
 * 移动端 p-4、桌面 p-6;根容器 min-w-0 保证 flex 子项可收缩,
 * 避免 390px 视口下侧栏/表格把主内容顶出视口。
 */
export function AdminPageShell({ children, className }: { children: ReactNode; className?: string }) {
  return <main className={cn('grid min-w-0 content-start gap-5 p-4 md:p-6', className)}>{children}</main>;
}

/**
 * 页面主标题区。每页只保留一个 AdminPageTitle(与 AppHeader 标题区不重复
 * 承担"页面主标题"角色,避免出现两个同级大标题),区块标题用 AdminSection。
 */
export function AdminPageTitle({
  title,
  description,
  actions,
}: {
  title: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <div className="flex flex-wrap items-center gap-3">
      <div className="min-w-0">
        <h1 className="text-xl font-semibold">{title}</h1>
        {description != null && <p className="mt-1 text-xs text-muted-foreground">{description}</p>}
      </div>
      {actions != null && <div className="ml-auto flex flex-wrap items-center gap-2">{actions}</div>}
    </div>
  );
}

/** 统一内容区块:圆角、边框、白底、标题区与说明区,窄屏间距一致。 */
export function AdminSection({
  title,
  description,
  actions,
  children,
  className,
  bodyClassName,
}: {
  title?: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
  bodyClassName?: string;
}) {
  return (
    <section className={cn('grid min-w-0 gap-4 rounded-xl border bg-white p-4 shadow-sm', className)}>
      {(title != null || actions != null) && (
        <div className="flex flex-wrap items-center gap-3">
          <div className="min-w-0">
            {title != null && <h2 className="font-semibold">{title}</h2>}
            {description != null && <p className="mt-1 text-xs text-muted-foreground">{description}</p>}
          </div>
          {actions != null && <div className="ml-auto flex flex-wrap items-center gap-2">{actions}</div>}
        </div>
      )}
      <div className={cn('grid min-w-0 gap-3', bodyClassName)}>{children}</div>
    </section>
  );
}
