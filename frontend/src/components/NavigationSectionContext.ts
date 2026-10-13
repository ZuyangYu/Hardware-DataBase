import { createContext } from 'react';
import type { NavItem, PrimaryNavItem } from '@/lib/navigation';

export type NavigationContextValue = {
  sectionLabel?: string;
  primary?: PrimaryNavItem;
  subnav?: NavItem[];
};

/** Shell 根据权限过滤后的一级/二级导航派生信息。 */
export const NavigationSectionContext = createContext<NavigationContextValue | undefined>(undefined);
