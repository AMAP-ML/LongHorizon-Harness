import React from 'react';
import { createRoot } from 'react-dom/client';
import App from './App';
import { UiLanguageProvider } from './i18n';
import './style.css';

createRoot(document.getElementById('root')!).render(
  <React.StrictMode><UiLanguageProvider><App /></UiLanguageProvider></React.StrictMode>,
);
