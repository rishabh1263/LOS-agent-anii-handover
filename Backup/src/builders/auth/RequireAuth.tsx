import { Loader2 } from 'lucide-react'
import { useAuth } from '../../runtime/auth'
import { LoginPage } from './LoginPage'

/**
 * Renders children only when the user has a valid session.
 * Empty or expired token (after refresh failure) shows the login page.
 * While the auth store is hydrating, shows a brief loading state to avoid flicker.
 */
export function RequireAuth({ children }: { children: React.ReactNode }) {
  const { isAuthenticated, isLoading } = useAuth()

  if (isLoading) {
    return (
      <div
        className="flex min-h-[calc(100vh-5rem)] items-center justify-center"
        role="status"
        aria-live="polite"
      >
        <div className="flex items-center gap-2 text-content-secondary">
          <Loader2 className="h-5 w-5 animate-spin" aria-hidden />
          <span className="font-sans text-[14px]">Checking session…</span>
        </div>
      </div>
    )
  }

  if (!isAuthenticated) {
    return <LoginPage />
  }

  return <>{children}</>
}
