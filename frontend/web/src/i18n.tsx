import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from 'react';

export type UiLanguage = 'zh' | 'en';

const STORAGE_KEY = 'lh-ui-language';

function initialLanguage(): UiLanguage {
  try {
    const stored = window.localStorage.getItem(STORAGE_KEY);
    if (stored === 'zh' || stored === 'en') return stored;
  } catch {
    // Storage can be disabled without making the workbench unusable.
  }
  return navigator.language.toLowerCase().startsWith('zh') ? 'zh' : 'en';
}

type UiLanguageContextValue = {
  language: UiLanguage;
  setLanguage: (language: UiLanguage) => void;
  text: (zh: string, en: string) => string;
};

const UiLanguageContext = createContext<UiLanguageContextValue | null>(null);

export function UiLanguageProvider({ children }: { children: ReactNode }) {
  const [language, setLanguage] = useState<UiLanguage>(initialLanguage);

  useEffect(() => {
    document.documentElement.lang = language === 'zh' ? 'zh-CN' : 'en';
    try {
      window.localStorage.setItem(STORAGE_KEY, language);
    } catch {
      // A disabled storage backend should not break language switching.
    }
  }, [language]);

  const value = useMemo<UiLanguageContextValue>(() => ({
    language,
    setLanguage,
    text: (zh, en) => language === 'zh' ? zh : en,
  }), [language]);

  return <UiLanguageContext.Provider value={value}>{children}</UiLanguageContext.Provider>;
}

export function useUiLanguage(): UiLanguageContextValue {
  const value = useContext(UiLanguageContext);
  if (!value) throw new Error('useUiLanguage must be used inside UiLanguageProvider');
  return value;
}

export function uiText(language: UiLanguage, zh: string, en: string): string {
  return language === 'zh' ? zh : en;
}
