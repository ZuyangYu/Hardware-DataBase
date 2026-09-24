import type { ReactNode } from 'react';

/**
 * i18n 桩:本应用中文 only,不搬 MutationObserver + en.json 翻译机制。
 * 只提供 timezone.ts / 适配组件需要的导出,让它们编译通过。
 * `t` 原样返回中文串;locale 恒为 zh-CN。
 */

export type Locale = 'zh-CN' | 'en-US';

export function getStoredLocale(): Locale {
  return 'zh-CN';
}

export function getDateLocale(): Locale {
  return 'zh-CN';
}

type UseI18n = {
  locale: Locale;
  setLocale: (locale: Locale) => void;
  toggleLocale: () => void;
  t: (text: string) => string;
};

export function useI18n(): UseI18n {
  return {
    locale: 'zh-CN',
    setLocale: () => undefined,
    toggleLocale: () => undefined,
    t: (text: string) => text,
  };
}

export function I18nProvider({ children }: { children: ReactNode }) {
  return <>{children}</>;
}
