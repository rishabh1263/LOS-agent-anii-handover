import { useEffect, useState } from "react";
import { LogOut, Moon, Sun, User as UserIcon, Webhook } from "lucide-react";
import { ProcessPage } from "./builders/api-tester";
import { LoginPage } from "./builders/auth";
import { AuthProvider, useAuth } from "./runtime/auth";

function AppContent() {
  const { isAuthenticated, user, logout } = useAuth();
  const [theme, setTheme] = useState<"light" | "dark">(() => {
    const saved = localStorage.getItem("los-theme");
    if (saved === "dark" || saved === "light") return saved;
    return "light"; // Default to clean light mode as requested by user
  });

  useEffect(() => {
    document.documentElement.setAttribute("data-theme", theme);
    localStorage.setItem("los-theme", theme);
  }, [theme]);

  const toggleTheme = () => {
    setTheme((prev) => (prev === "light" ? "dark" : "light"));
  };

  return (
    <div className="min-h-screen bg-canvas text-content font-sans antialiased transition-colors duration-150">
      {/* Top Navigation Bar according to Ink & Ember design system */}
      <header className="sticky top-0 z-30 border-b border-line bg-surface">
        <div className="mx-auto flex h-14 max-w-4xl items-center justify-between px-4 sm:h-16 sm:px-6">
          <div className="flex items-center gap-3">
            {/* Logo Mark: neutral raised well with 3px ember mark */}
            <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-sm border border-line border-l-[3px] border-l-ember bg-raised">
              <span className="font-display text-[15px] font-bold text-content">
                <Webhook size={18} strokeWidth={2.2} />
              </span>
            </div>
            <div>
              <div className="flex items-center gap-2">
                <h1 className="font-display text-[17px] font-bold tracking-tight text-content">
                  LOS Process
                </h1>
              </div>
              <p className="text-[12px] font-normal text-content-secondary">
                Document verification
              </p>
            </div>
          </div>

          {/* Right side controls */}
          <div className="flex items-center gap-2 sm:gap-3">
            <div className="chip">
              <span className="h-1.5 w-1.5 rounded-full bg-success" />
              <span className="hidden xs:inline">API Online</span>
            </div>

            {isAuthenticated && user && (
              <div className="chip hidden md:inline-flex items-center gap-1.5 font-mono text-[12px]">
                <UserIcon className="h-3.5 w-3.5 text-ember" />
                <span>{user.username}</span>
              </div>
            )}

            <button
              type="button"
              onClick={toggleTheme}
              aria-label={`Switch to ${theme === "light" ? "dark" : "light"} mode`}
              className="flex h-9 items-center gap-1.5 rounded-sm border border-line bg-surface px-2.5 text-[12px] font-medium text-content transition-colors duration-150 hover:bg-raised hover:border-line-strong focus:outline-none focus-visible:ring-2 focus-visible:ring-ember"
            >
              {theme === "light" ? (
                <>
                  <Sun
                    className="h-4 w-4 text-icon-default"
                    aria-hidden="true"
                  />
                  <span className="hidden sm:inline">Light</span>
                </>
              ) : (
                <>
                  <Moon
                    className="h-4 w-4 text-icon-default"
                    aria-hidden="true"
                  />
                  <span className="hidden sm:inline">Dark</span>
                </>
              )}
            </button>

            {isAuthenticated && (
              <button
                type="button"
                onClick={logout}
                aria-label="Sign out"
                title="Sign out"
                className="flex h-9 items-center gap-1.5 rounded-sm border border-line bg-surface px-2.5 text-[12px] font-medium text-danger hover:bg-danger-subtle hover:border-danger/30 transition-colors duration-150 focus:outline-none focus-visible:ring-2 focus-visible:ring-danger"
              >
                <LogOut className="h-4 w-4" />
                <span className="hidden sm:inline">Logout</span>
              </button>
            )}
          </div>
        </div>
      </header>

      {/* Main Content Area */}
      <main className="mx-auto max-w-4xl px-4 py-6 sm:px-6 sm:py-8">
        {isAuthenticated ? <ProcessPage /> : <LoginPage />}
      </main>
    </div>
  );
}

export default function App() {
  return (
    <AuthProvider>
      <AppContent />
    </AuthProvider>
  );
}
