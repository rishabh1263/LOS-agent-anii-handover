import React, { useState } from 'react'
import { AlertCircle, Eye, EyeOff, KeyRound, Loader2, Lock, User } from 'lucide-react'
import { useAuth, AuthApiError } from '../../runtime/auth'

export function LoginPage() {
  const { login, isLoading } = useAuth()
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [showPassword, setShowPassword] = useState(false)
  const [errorMessage, setErrorMessage] = useState<string | null>(null)

  const handleFillDemo = () => {
    setUsername('AniketDev')
    setPassword('Dev@123')
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
      await login({
        username: trimmedUsername,
        password,
      })
    } catch (err) {
      if (err instanceof AuthApiError) {
        setErrorMessage(err.detail)
      } else if (err instanceof Error) {
        setErrorMessage(err.message)
      } else {
        setErrorMessage('Failed to log in. Please try again.')
      }
    }
  }

  return (
    <div className="flex min-h-[calc(100vh-5rem)] items-center justify-center py-10 px-4 sm:px-6">
      <div className="w-full max-w-md space-y-6">
        {/* Login Card */}
        <div className="card shadow-sm border border-line bg-surface p-6 sm:p-8">
          <form onSubmit={handleSubmit} className="space-y-4" noValidate>
            {/* Error Message */}
            {errorMessage && (
              <div
                role="alert"
                className="flex items-start gap-2.5 rounded-sm border border-danger/30 bg-danger-subtle p-3 text-[13px] text-danger-text animate-in fade-in"
              >
                <AlertCircle className="h-4 w-4 shrink-0 mt-0.5" />
                <span className="leading-snug">{errorMessage}</span>
              </div>
            )}

            {/* Username Field */}
            <div>
              <label htmlFor="login-username" className="label flex items-center justify-between">
                <span>Username</span>
              </label>
              <div className="relative">
                <div className="pointer-events-none absolute inset-y-0 left-0 flex items-center pl-3 text-content-secondary">
                  <User className="h-4 w-4" />
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
                />
              </div>
            </div>

            {/* Password Field */}
            <div>
              <label htmlFor="login-password" className="label flex items-center justify-between">
                <span>Password</span>
              </label>
              <div className="relative">
                <div className="pointer-events-none absolute inset-y-0 left-0 flex items-center pl-3 text-content-secondary">
                  <Lock className="h-4 w-4" />
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
                />
                <button
                  type="button"
                  onClick={() => setShowPassword((prev) => !prev)}
                  tabIndex={-1}
                  aria-label={showPassword ? 'Hide password' : 'Show password'}
                  className="absolute inset-y-0 right-0 flex items-center pr-3 text-content-secondary hover:text-content transition-colors focus:outline-none"
                >
                  {showPassword ? (
                    <EyeOff className="h-4 w-4" />
                  ) : (
                    <Eye className="h-4 w-4" />
                  )}
                </button>
              </div>
            </div>

            {/* Submit Button */}
            <div className="pt-2">
              <button
                type="submit"
                disabled={isLoading}
                className="btn btn-accent w-full justify-center shadow-xs"
              >
                {isLoading ? (
                  <>
                    <Loader2 className="h-4 w-4 animate-spin" />
                    <span>Signing in...</span>
                  </>
                ) : (
                  <>
                    <KeyRound className="h-4 w-4" />
                    <span>Sign In</span>
                  </>
                )}
              </button>
            </div>
          </form>

          {/* Quick Demo Credential Autofill */}
          <div className="mt-6 border-t border-line-divider pt-4">
            <div className="flex items-center justify-between">
              <span className="text-[12px] text-content-secondary">
                Test environment credentials:
              </span>
              <button
                type="button"
                onClick={handleFillDemo}
                className="chip hover:bg-raised-hover hover:text-content transition-colors cursor-pointer text-[11px]"
                title="Fill dummy username & password"
              >
                Auto-fill Demo
              </button>
            </div>
            <div className="mt-2 flex items-center justify-between rounded-xs bg-raised px-3 py-1.5 font-mono text-[12px] text-content-secondary">
              <span>AniketDev</span>
              <span className="text-content-disabled">• • • • • • •</span>
            </div>
          </div>
        </div>

        {/* Security / System Footer */}
        <div className="text-center text-[11px] text-content-disabled">
          Protected by rate limiting & Dev IDP token rotation.
        </div>
      </div>
    </div>
  )
}
