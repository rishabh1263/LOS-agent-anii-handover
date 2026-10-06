import React, { useState } from 'react'
import {
  AlertCircle,
  Eye,
  EyeOff,
  KeyRound,
  Loader2,
  Lock,
  User,
} from 'lucide-react'
import { useAuth, AuthApiError, AUTH_STAGES, type AuthStage } from '../../runtime/auth'

/**
 * Login page — Step 1 of 2.
 *
 * Validates username + password + stage only.
 * Case ID / APP ID entry is a SEPARATE step shown AFTER successful login
 * (handled by CaseSelectPage / ProcessPage).
 * No case data is linked to the logged-in user here.
 */
export function LoginPage() {
  const { login, isLoading } = useAuth()
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [stage, setStage] = useState<AuthStage>('FOS')
  const [showPassword, setShowPassword] = useState(false)
  const [errorMessage, setErrorMessage] = useState<string | null>(null)

  const handleFillDemo = () => {
    setUsername('AniketDev')
    setPassword('Dev@123')
    setStage('FOS')
    setErrorMessage(null)
  }

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setErrorMessage(null)

    const trimmedUsername = username.trim()
    if (!trimmedUsername) {
      setErrorMessage('Please enter your username.')
      return
    }
    if (!password) {
      setErrorMessage('Please enter your password.')
      return
    }

    try {
      await login({ username: trimmedUsername, password, stage })
      // On success, AuthProvider sets isAuthenticated = true.
      // App.tsx renders ProcessPage (via RequireAuth) which shows the
      // Case ID / APP ID selection screen as the next step.
    } catch (err) {
      if (err instanceof AuthApiError) {
        if (err.status === 401 || err.status === 403) {
          setErrorMessage('Invalid username or password.')
        } else if (err.status === 429) {
          setErrorMessage('Too many attempts. Please wait and try again.')
        } else if (err.status >= 500) {
          setErrorMessage('Server error. Please try again later.')
        } else {
          setErrorMessage(err.detail || 'Failed to log in. Please try again.')
        }
      } else if (err instanceof TypeError) {
        setErrorMessage('Network error. Check your connection and try again.')
      } else if (err instanceof Error) {
        setErrorMessage(err.message || 'Failed to log in. Please try again.')
      } else {
        setErrorMessage('Failed to log in. Please try again.')
      }
    }
  }

  return (
    <div className="flex min-h-[calc(100vh-5rem)] items-center justify-center px-4 py-10 sm:px-6">
      <div className="w-full max-w-md space-y-6">
        <div className="card border border-line bg-surface p-6 shadow-sm sm:p-8">
          <div className="mb-6">
            <h2 className="font-display text-lg font-semibold tracking-tight text-content">
              Sign in
            </h2>
            <p className="mt-1 text-[13px] text-content-secondary">
              Use your LOS credentials to continue. You will select a Case ID and APP ID after
              signing in.
            </p>
          </div>

          <form onSubmit={handleSubmit} className="space-y-4" noValidate>
            {errorMessage && (
              <div
                role="alert"
                className="flex items-start gap-2.5 rounded-sm border border-danger/30 bg-danger-subtle p-3 text-[13px] text-danger-text"
              >
                <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden />
                <span className="leading-snug">{errorMessage}</span>
              </div>
            )}

            {/* Stage */}
            <div>
              <label htmlFor="login-stage" className="label">
                Stage
              </label>
              <select
                id="login-stage"
                name="stage"
                value={stage}
                disabled={isLoading}
                onChange={(e) => setStage(e.target.value as AuthStage)}
                className="input"
                required
              >
                {AUTH_STAGES.map((s) => (
                  <option key={s.value} value={s.value}>
                    {s.label}
                  </option>
                ))}
              </select>
              <p className="mt-1 text-[12px] text-content-secondary">
                Workflow context (FOS, CPA, HOPS, BOPS, Credit).
              </p>
            </div>

            {/* Username */}
            <div>
              <label htmlFor="login-username" className="label">
                Username
              </label>
              <div className="relative">
                <div className="pointer-events-none absolute inset-y-0 left-0 flex items-center pl-3 text-content-secondary">
                  <User className="h-4 w-4" aria-hidden />
                </div>
                <input
                  id="login-username"
                  name="username"
                  type="text"
                  autoComplete="username"
                  autoFocus
                  required
                  disabled={isLoading}
                  value={username}
                  onChange={(e) => setUsername(e.target.value)}
                  placeholder="e.g. AniketDev"
                  className="input pl-9"
                  aria-invalid={Boolean(errorMessage && !username.trim())}
                />
              </div>
            </div>

            {/* Password */}
            <div>
              <label htmlFor="login-password" className="label">
                Password
              </label>
              <div className="relative">
                <div className="pointer-events-none absolute inset-y-0 left-0 flex items-center pl-3 text-content-secondary">
                  <Lock className="h-4 w-4" aria-hidden />
                </div>
                <input
                  id="login-password"
                  name="password"
                  type={showPassword ? 'text' : 'password'}
                  autoComplete="current-password"
                  required
                  disabled={isLoading}
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  placeholder="Enter your password"
                  className="input pl-9 pr-10"
                  aria-invalid={Boolean(errorMessage && !password)}
                />
                <button
                  type="button"
                  onClick={() => setShowPassword((prev) => !prev)}
                  tabIndex={-1}
                  aria-label={showPassword ? 'Hide password' : 'Show password'}
                  className="absolute inset-y-0 right-0 flex items-center rounded-sm pr-3 text-content-secondary transition-colors hover:text-content focus:outline-none focus-visible:ring-2 focus-visible:ring-ember"
                >
                  {showPassword ? (
                    <EyeOff className="h-4 w-4" aria-hidden />
                  ) : (
                    <Eye className="h-4 w-4" aria-hidden />
                  )}
                </button>
              </div>
            </div>

            <div className="pt-2">
              <button
                type="submit"
                disabled={isLoading}
                className="btn btn-accent w-full justify-center shadow-xs"
              >
                {isLoading ? (
                  <>
                    <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
                    <span>Signing in…</span>
                  </>
                ) : (
                  <>
                    <KeyRound className="h-4 w-4" aria-hidden />
                    <span>Sign In</span>
                  </>
                )}
              </button>
            </div>
          </form>

          {import.meta.env.DEV && (
            <div className="mt-6 border-t border-line-divider pt-4">
              <div className="flex items-center justify-between gap-2">
                <span className="text-[12px] text-content-secondary">
                  Test credentials (dev only)
                </span>
                <button
                  type="button"
                  onClick={handleFillDemo}
                  className="chip cursor-pointer text-[11px] transition-colors hover:bg-raised-hover hover:text-content"
                  title="Fill demo username & password"
                >
                  Auto-fill Demo
                </button>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
