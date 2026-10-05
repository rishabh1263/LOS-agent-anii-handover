import React, { useState } from 'react'
import {
  AlertCircle,
  Eye,
  EyeOff,
  FolderOpen,
  KeyRound,
  Loader2,
  Lock,
  User,
} from 'lucide-react'
import { useAuth, AuthApiError, AUTH_STAGES, type AuthStage } from '../../runtime/auth'
import { savePendingCaseResume } from '../../runtime/api-tester'

type LoginMode = 'new' | 'resume'

export function LoginPage() {
  const { login, isLoading } = useAuth()
  const [mode, setMode] = useState<LoginMode>('new')
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [stage, setStage] = useState<AuthStage>('FOS')
  const [applicantId, setApplicantId] = useState('')
  const [caseId, setCaseId] = useState('')
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

    if (mode === 'resume') {
      const appId = applicantId.trim()
      const cId = caseId.trim()
      if (!appId) {
        setErrorMessage('Please enter the Applicant ID (APP id).')
        return
      }
      if (!cId) {
        setErrorMessage('Please enter the Case ID.')
        return
      }
    }

    try {
      if (mode === 'resume') {
        savePendingCaseResume(applicantId.trim(), caseId.trim())
      }
      await login({
        username: trimmedUsername,
        password,
        stage,
      })
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
              Use your LOS credentials to continue.
            </p>
          </div>

          {/* Mode toggle */}
          <div
            className="mb-5 grid grid-cols-2 gap-1 rounded-lg border border-line bg-raised p-1"
            role="tablist"
            aria-label="Login mode"
          >
            <button
              type="button"
              role="tab"
              aria-selected={mode === 'new'}
              disabled={isLoading}
              onClick={() => {
                setMode('new')
                setErrorMessage(null)
              }}
              className={`cursor-pointer rounded-md px-3 py-2 text-[12.5px] font-semibold transition focus:outline-none focus-visible:ring-2 focus-visible:ring-ember disabled:opacity-50 ${
                mode === 'new'
                  ? 'bg-surface text-content shadow-xs'
                  : 'text-content-secondary hover:text-content'
              }`}
            >
              New session
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={mode === 'resume'}
              disabled={isLoading}
              onClick={() => {
                setMode('resume')
                setErrorMessage(null)
              }}
              className={`inline-flex cursor-pointer items-center justify-center gap-1.5 rounded-md px-3 py-2 text-[12.5px] font-semibold transition focus:outline-none focus-visible:ring-2 focus-visible:ring-ember disabled:opacity-50 ${
                mode === 'resume'
                  ? 'bg-surface text-content shadow-xs'
                  : 'text-content-secondary hover:text-content'
              }`}
            >
              <FolderOpen className="h-3.5 w-3.5" strokeWidth={2} aria-hidden />
              Existing case
            </button>
          </div>

          {mode === 'resume' && (
            <div className="mb-4 rounded-lg border border-ember/20 bg-ember/[0.04] px-3 py-2.5 text-[12px] leading-snug text-content-secondary">
              Sign in with credentials, then open an existing{' '}
              <span className="font-medium text-content">Applicant ID</span> and{' '}
              <span className="font-medium text-content">Case ID</span> to load checklist and
              continue verification.
            </div>
          )}

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

            {mode === 'resume' && (
              <>
                <div>
                  <label htmlFor="login-applicant-id" className="label">
                    Applicant ID
                  </label>
                  <input
                    id="login-applicant-id"
                    name="applicantId"
                    type="text"
                    autoComplete="off"
                    required
                    disabled={isLoading}
                    value={applicantId}
                    onChange={(e) => setApplicantId(e.target.value)}
                    placeholder="e.g. APP-xxxx or applicant uuid"
                    className="input font-mono text-[13px]"
                    aria-invalid={Boolean(errorMessage && mode === 'resume' && !applicantId.trim())}
                  />
                </div>
                <div>
                  <label htmlFor="login-case-id" className="label">
                    Case ID
                  </label>
                  <input
                    id="login-case-id"
                    name="caseId"
                    type="text"
                    autoComplete="off"
                    required
                    disabled={isLoading}
                    value={caseId}
                    onChange={(e) => setCaseId(e.target.value)}
                    placeholder="e.g. CASE-xxxx or case uuid"
                    className="input font-mono text-[13px]"
                    aria-invalid={Boolean(errorMessage && mode === 'resume' && !caseId.trim())}
                  />
                </div>
              </>
            )}

            <div className="pt-2">
              <button
                type="submit"
                disabled={isLoading}
                className="btn btn-accent w-full justify-center shadow-xs"
              >
                {isLoading ? (
                  <>
                    <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
                    <span>{mode === 'resume' ? 'Signing in & loading…' : 'Signing in…'}</span>
                  </>
                ) : mode === 'resume' ? (
                  <>
                    <FolderOpen className="h-4 w-4" aria-hidden />
                    <span>Sign in & open case</span>
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
