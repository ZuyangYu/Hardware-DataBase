import React from 'react';
import ReactDOM from 'react-dom/client';
import App from './App';
import { I18nProvider } from './i18n';
import './styles.css';

// Visible in DevTools as `document.documentElement.dataset.uiBuild`; makes it
// unambiguous whether a reported UI issue comes from the current checkout or a
// stale tab/proxy cache.
const UI_BUILD = '2026-10-30-no-plan-grep-pointer';
document.documentElement.dataset.uiBuild = UI_BUILD;
console.info(`[HDB UI] ${UI_BUILD}`);

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <I18nProvider>
      <App />
    </I18nProvider>
  </React.StrictMode>,
);
