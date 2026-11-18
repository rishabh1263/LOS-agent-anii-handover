// Runs before the page paints (not deferred), so a saved theme never flashes the other one first.
// "light" / "dark" = the person's choice on this browser; nothing saved = follow the system setting.
"use strict";
try {
  const saved = localStorage.getItem("docs-credit-theme");
  if (saved === "light" || saved === "dark") document.documentElement.dataset.theme = saved;
} catch { /* private window: follow the system */ }
