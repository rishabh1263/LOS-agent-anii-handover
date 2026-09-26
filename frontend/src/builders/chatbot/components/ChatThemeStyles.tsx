/**
 * Chatbot-only themes (does not affect the rest of the app).
 * Light / Dark / Orange (WhatsApp-style accent).
 */

export type ChatThemeId = 'light' | 'dark' | 'orange'

const CSS = `
.chatbot-shell {
  color-scheme: light;
  --cb-surface: #ffffff;
  --cb-raised: #f0f2f5;
  --cb-content: #111b21;
  --cb-secondary: #667781;
  --cb-disabled: #8696a0;
  --cb-line: #e9edef;
  --cb-divider: #f0f2f5;
  --cb-accent: #25d366;
  --cb-accent-text: #128c7e;
  --cb-on-accent: #ffffff;
  --cb-bubble-user: #d9fdd3;
  --cb-bubble-ai: #ffffff;
  --cb-header: #f0f2f5;
  --cb-danger: #ea0038;
  --cb-warning: #b54708;
  background-color: var(--cb-surface);
  color: var(--cb-content);
  border-color: var(--cb-line);
}

.chatbot-shell[data-chat-theme="dark"] {
  color-scheme: dark;
  --cb-surface: #0b141a;
  --cb-raised: #111b21;
  --cb-content: #e9edef;
  --cb-secondary: #8696a0;
  --cb-disabled: #667781;
  --cb-line: #2a3942;
  --cb-divider: #1f2c33;
  --cb-accent: #00a884;
  --cb-accent-text: #00a884;
  --cb-on-accent: #0b141a;
  --cb-bubble-user: #005c4b;
  --cb-bubble-ai: #202c33;
  --cb-header: #202c33;
  --cb-danger: #f15c6d;
  --cb-warning: #e9c46a;
}

.chatbot-shell[data-chat-theme="orange"] {
  color-scheme: light;
  --cb-surface: #fffaf5;
  --cb-raised: #fff3e8;
  --cb-content: #1c1410;
  --cb-secondary: #8a6a55;
  --cb-disabled: #b08f78;
  --cb-line: #f0dcc8;
  --cb-divider: #f7ebe0;
  --cb-accent: #ff6b00;
  --cb-accent-text: #e65c00;
  --cb-on-accent: #ffffff;
  --cb-bubble-user: #ffe0c2;
  --cb-bubble-ai: #ffffff;
  --cb-header: #fff3e8;
  --cb-danger: #d92d20;
  --cb-warning: #b54708;
}

.chatbot-shell[data-chat-theme="dark"].chatbot-shell--orange-dark,
.chatbot-shell[data-chat-theme="orange"][data-chat-dark="true"] {
  /* reserved */
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
