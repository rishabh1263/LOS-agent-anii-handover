import { useEffect, useState } from 'react'
import { LogOut, Moon, Sun, User as UserIcon, Webhook } from 'lucide-react'
import { ProcessPage, CaseSelectPage } from './builders/api-tester'
import { RequireAuth } from './builders/auth'
import { AuthProvider, useAuth } from './runtime/auth'
import type { CaseDataResponse } from './runtime/api-tester'

// ---------------------------------------------------------------------------
// Header
// ---------------------------------------------------------------------------

function AppHeader() {
  const { isAuthenticated, user, logout, isLoading } = useAuth()
  const [theme, setTheme] = useState<'light' | 'dark'>(() => {
    try {
      const saved = localStorage.getItem('los-theme')
      if (saved === 'dark' || saved === 'light') return saved
    } catch {
      /* ignore */
    }
    return 'light'
  })

  useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme)
    try {
      localStorage.setItem('los-theme', theme)
    } catch {
      /* ignore */
    }
  }, [theme])

  const toggleTheme = () => {
    setTheme((prev) => (prev === 'light' ? 'dark' : 'light'))
  }

  const handleLogout = async () => {
    try {
      await logout()
    } catch {
      /* session already cleared in AuthProvider */
    }
  }

  return (
    <header className="sticky top-0 z-30 border-b border-line bg-surface">
      <div className="mx-auto flex h-14 max-w-4xl items-center justify-between px-4 sm:h-16 sm:px-6">
        <div className="flex items-center gap-3">
          <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-sm border border-line border-l-[3px] border-l-ember bg-raised">
            <Webhook size={18} strokeWidth={2.2} aria-hidden />
          </div>
          <div>
            <h1 className="font-display text-[17px] font-bold tracking-tight text-content">
              LOS Process
            </h1>
            <p className="text-[12px] font-normal text-content-secondary">
              Document verification
            </p>
          </div>
        </div>

        <div className="flex items-center gap-2 sm:gap-3">
          <div className="chip">
            <span className="h-1.5 w-1.5 rounded-full bg-success" aria-hidden />
            <span className="hidden xs:inline">API Online</span>
          </div>

          {isAuthenticated && user && (
            <div className="chip hidden md:inline-flex items-center gap-1.5 font-mono text-[12px]">
              <UserIcon className="h-3.5 w-3.5 text-ember" aria-hidden />
              <span>{user.username}</span>
              {user.stage && (
                <span className="text-content-secondary">· {user.stage}</span>
              )}
            </div>
          )}

          <button
            type="button"
            onClick={toggleTheme}
            aria-label={`Switch to ${theme === 'light' ? 'dark' : 'light'} mode`}
            className="flex h-9 items-center gap-1.5 rounded-sm border border-line bg-surface px-2.5 text-[12px] font-medium text-content transition-colors duration-150 hover:bg-raised hover:border-line-strong focus:outline-none focus-visible:ring-2 focus-visible:ring-ember"
          >
            {theme === 'light' ? (
              <>
                <Sun className="h-4 w-4 text-icon-default" aria-hidden />
                <span className="hidden sm:inline">Light</span>
              </>
            ) : (
              <>
                <Moon className="h-4 w-4 text-icon-default" aria-hidden />
                <span className="hidden sm:inline">Dark</span>
              </>
            )}
          </button>

          {isAuthenticated && (
            <button
              type="button"
              onClick={handleLogout}
              disabled={isLoading}
              aria-label="Sign out"
              title="Sign out"
              className="flex h-9 items-center gap-1.5 rounded-sm border border-line bg-surface px-2.5 text-[12px] font-medium text-danger hover:bg-danger-subtle hover:border-danger/30 transition-colors duration-150 focus:outline-none focus-visible:ring-2 focus-visible:ring-danger disabled:opacity-60"
            >
              <LogOut className="h-4 w-4" aria-hidden />
              <span className="hidden sm:inline">Logout</span>
            </button>
          )}
        </div>
      </div>
    </header>
  )
}

// ---------------------------------------------------------------------------
// Two-step post-login flow:
//   Step 1 (case-select): user enters Case ID + APP ID  OR  chooses New Case
//   Step 2 (process):     KYC wizard, pre-loaded with case data if provided
// ---------------------------------------------------------------------------

type AppStep = 'case-select' | 'process'

function AppContent() {
  const { isAuthenticated, accessToken } = useAuth()
  const [appStep, setAppStep] = useState<AppStep>('case-select')
  const [loadedCaseData, setLoadedCaseData] = useState<CaseDataResponse | null>(null)

  // Reset to case-select whenever user logs out and back in
  useEffect(() => {
    if (!isAuthenticated) {
      setAppStep('case-select')
      setLoadedCaseData(null)
    }
  }, [isAuthenticated])

  function handleNewCase() {
    setLoadedCaseData(null)
    setAppStep('process')
  }

  function handleCaseLoaded(data: CaseDataResponse) {
    setLoadedCaseData(data)
    setAppStep('process')
  }

  function handleBackToCaseSelect() {
    setLoadedCaseData(null)
    setAppStep('case-select')
  }

  return (
    <div className="min-h-screen bg-canvas text-content font-sans antialiased transition-colors duration-150">
      <AppHeader />
      <main className="mx-auto max-w-4xl px-4 py-6 sm:px-6 sm:py-8">
        <RequireAuth>
          {appStep === 'case-select' ? (
            <CaseSelectPage
              accessToken={accessToken || ''}
              onNewCase={handleNewCase}
              onCaseLoaded={handleCaseLoaded}
            />
          ) : (
            <ProcessPage
              loadedCaseData={loadedCaseData}
              onBackToCaseSelect={handleBackToCaseSelect}
            />
          )}
        </RequireAuth>
      </main>
    </div>
  )
}

export default function App() {
  return (
    <AuthProvider>
      <AppContent />
    </AuthProvider>
  )
}
