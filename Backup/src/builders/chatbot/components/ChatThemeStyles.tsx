/**
 * Chatbot-only themes (does not affect the rest of the app).
 * Clean professional palette — Light / Dark / Accent.
 */

export type ChatThemeId = 'light' | 'dark' | 'orange'

const CSS = `
.chatbot-shell {
  color-scheme: light;
  --cb-surface: #ffffff;
  --cb-raised: #f7f7f8;
  --cb-content: #0d0d0d;
  --cb-secondary: #6b6b6b;
  --cb-disabled: #a0a0a0;
  --cb-line: #e5e5e5;
  --cb-divider: #f0f0f0;
  --cb-accent: #2563eb;
  --cb-accent-text: #1d4ed8;
  --cb-on-accent: #ffffff;
  --cb-bubble-user: #2563eb;
  --cb-bubble-user-text: #ffffff;
  --cb-bubble-ai: #f4f4f5;
  --cb-header: #ffffff;
  --cb-danger: #dc2626;
  --cb-warning: #b45309;
  --cb-success: #16a34a;
  background-color: var(--cb-surface);
  color: var(--cb-content);
  border-color: var(--cb-line);
}

.chatbot-shell[data-chat-theme="dark"] {
  color-scheme: dark;
  --cb-surface: #0f0f0f;
  --cb-raised: #1a1a1a;
  --cb-content: #f5f5f5;
  --cb-secondary: #a3a3a3;
  --cb-disabled: #737373;
  --cb-line: #2a2a2a;
  --cb-divider: #1f1f1f;
  --cb-accent: #3b82f6;
  --cb-accent-text: #60a5fa;
  --cb-on-accent: #ffffff;
  --cb-bubble-user: #2563eb;
  --cb-bubble-user-text: #ffffff;
  --cb-bubble-ai: #1c1c1c;
  --cb-header: #141414;
  --cb-danger: #f87171;
  --cb-warning: #fbbf24;
  --cb-success: #4ade80;
}

.chatbot-shell[data-chat-theme="orange"] {
  color-scheme: light;
  --cb-surface: #fffdfb;
  --cb-raised: #faf6f2;
  --cb-content: #1a120e;
  --cb-secondary: #7a6558;
  --cb-disabled: #b09a8a;
  --cb-line: #ede4db;
  --cb-divider: #f5efe9;
  --cb-accent: #ea580c;
  --cb-accent-text: #c2410c;
  --cb-on-accent: #ffffff;
  --cb-bubble-user: #ea580c;
  --cb-bubble-user-text: #ffffff;
  --cb-bubble-ai: #faf6f2;
  --cb-header: #fffdfb;
  --cb-danger: #dc2626;
  --cb-warning: #b45309;
  --cb-success: #16a34a;
}

/* Map common app utility classes inside chatbot only */
.chatbot-shell .bg-surface,
.chatbot-shell.bg-surface { background-color: var(--cb-surface) !important; }
.chatbot-shell .bg-surface\\/95 { background-color: color-mix(in srgb, var(--cb-surface) 95%, transparent) !important; }
.chatbot-shell .bg-raised,
.chatbot-shell .bg-raised\\/40 { background-color: var(--cb-raised) !important; }
.chatbot-shell .bg-raised-hover:hover { background-color: var(--cb-raised) !important; }
.chatbot-shell .text-content { color: var(--cb-content) !important; }
.chatbot-shell .text-content-secondary { color: var(--cb-secondary) !important; }
.chatbot-shell .text-content-disabled { color: var(--cb-disabled) !important; }
.chatbot-shell .border-line,
.chatbot-shell .border-line-strong { border-color: var(--cb-line) !important; }
.chatbot-shell .border-divider { border-color: var(--cb-divider) !important; }
.chatbot-shell .bg-ember,
.chatbot-shell .bg-ember-tint { background-color: var(--cb-accent) !important; }
.chatbot-shell .text-ember,
.chatbot-shell .text-ember-text { color: var(--cb-accent-text) !important; }
.chatbot-shell .border-ember,
.chatbot-shell .border-ember\\/40 { border-color: var(--cb-accent) !important; }
.chatbot-shell .ring-ember { --tw-ring-color: var(--cb-accent) !important; }
.chatbot-shell .focus-visible\\:ring-ember:focus-visible { --tw-ring-color: var(--cb-accent) !important; }
.chatbot-shell .text-danger,
.chatbot-shell .text-danger-text { color: var(--cb-danger) !important; }
.chatbot-shell .text-warning-text { color: var(--cb-warning) !important; }
.chatbot-shell .bg-oncolor { background-color: var(--cb-on-accent) !important; color: var(--cb-accent) !important; }

/* Composer / bubbles */
.chatbot-shell [data-role="user-bubble"] {
  background-color: var(--cb-bubble-user) !important;
  color: var(--cb-bubble-user-text) !important;
  border-color: transparent !important;
}
.chatbot-shell [data-role="ai-bubble"] {
  background-color: var(--cb-bubble-ai) !important;
}
.chatbot-shell [data-role="chat-header"] {
  background-color: var(--cb-header) !important;
  border-color: var(--cb-line) !important;
}
.chatbot-shell [data-role="chat-composer"] {
  background-color: var(--cb-surface) !important;
  border-color: var(--cb-line) !important;
}
.chatbot-shell input,
.chatbot-shell textarea,
.chatbot-shell select {
  background-color: var(--cb-raised);
  color: var(--cb-content);
  border-color: var(--cb-line);
}
.chatbot-shell .btn-primary {
  background-color: var(--cb-accent) !important;
  color: var(--cb-on-accent) !important;
  border-color: transparent !important;
}
`

let injected = false

export function ensureChatThemeStyles() {
  if (typeof document === 'undefined' || injected) return
  if (document.getElementById('chatbot-theme-styles')) {
    injected = true
    return
  }
  const el = document.createElement('style')
  el.id = 'chatbot-theme-styles'
  el.textContent = CSS
  document.head.appendChild(el)
  injected = true
}
