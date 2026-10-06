/**
 * CaseSelectPage.tsx
 *
 * Step 2 (after login) — shown BEFORE the KYC wizard.
 * User can either:
 *   A) Enter an existing Case ID + APP ID → API fetches data → wizard loads it
 *   B) Create a NEW CASE → wizard starts fresh (details step)
 *
 * Case/APP IDs are NOT tied to the logged-in user.
 * Any valid combination returns data regardless of who is logged in.
 */

import React, { useState } from 'react'
import { AlertCircle, FolderOpen, Loader2, Plus } from 'lucide-react'
import { fetchCaseData, CaseApiError } from '../../../runtime/api-tester'
import type { CaseDataResponse } from '../../../runtime/api-tester'

interface CaseSelectPageProps {
  accessToken: string
  /** Called when user wants a fresh new case (no IDs pre-loaded). */
  onNewCase: () => void
  /** Called when case data is successfully fetched. */
  onCaseLoaded: (data: CaseDataResponse) => void
}

export function CaseSelectPage({ accessToken, onNewCase, onCaseLoaded }: CaseSelectPageProps) {
  const [caseId, setCaseId] = useState('')
  const [appId, setAppId] = useState('')
  const [loading, setLoading] = useState(false)
  const [errorMessage, setErrorMessage] = useState<string | null>(null)

  const handleFetchCase = async (e: React.FormEvent) => {
    e.preventDefault()
    setErrorMessage(null)

    const trimmedCaseId = caseId.trim()
    const trimmedAppId = appId.trim()

    if (!trimmedCaseId) {
      setErrorMessage('Please enter a Case ID.')
      return
    }
    if (!trimmedAppId) {
      setErrorMessage('Please enter an APP ID.')
      return
    }

    setLoading(true)
    try {
      const data = await fetchCaseData(
        { case_id: trimmedCaseId, app_id: trimmedAppId },
        accessToken,
      )
      onCaseLoaded(data)
    } catch (err) {
      if (err instanceof CaseApiError) {
        if (err.status === 401 || err.status === 403) {
          setErrorMessage('Session expired. Please sign in again.')
        } else if (err.status === 404) {
          setErrorMessage('No case found for the given Case ID and APP ID.')
        } else if (err.status >= 500) {
          setErrorMessage('Server error. Please try again later.')
        } else {
          setErrorMessage(err.detail || 'Failed to fetch case data.')
        }
      } else if (err instanceof TypeError) {
        setErrorMessage('Network error. Check your connection and try again.')
      } else if (err instanceof Error) {
        setErrorMessage(err.message || 'Failed to fetch case data.')
      } else {
        setErrorMessage('Failed to fetch case data. Please try again.')
      }
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="flex min-h-[calc(100vh-5rem)] items-center justify-center px-4 py-10 sm:px-6">
      <div className="w-full max-w-md space-y-4">
        {/* ── New Case ─────────────────────────────────────────────── */}
        <div className="card border border-line bg-surface p-6 shadow-sm sm:p-8">
          <div className="mb-5">
            <h2 className="font-display text-lg font-semibold tracking-tight text-content">
              Select case
            </h2>
            <p className="mt-1 text-[13px] text-content-secondary">
              Open an existing case by entering its IDs, or start a brand-new case.
            </p>
          </div>

          {/* ── Open existing case form ──────────────────────────── */}
          <form onSubmit={handleFetchCase} className="space-y-4" noValidate>
            {errorMessage && (
              <div
                role="alert"
                className="flex items-start gap-2.5 rounded-sm border border-danger/30 bg-danger-subtle p-3 text-[13px] text-danger-text"
              >
                <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden />
                <span className="leading-snug">{errorMessage}</span>
              </div>
            )}

            <div className="rounded-lg border border-ember/20 bg-ember/[0.04] px-3 py-2.5 text-[12px] leading-snug text-content-secondary">
              <span className="font-medium text-content">Open existing case</span> — enter a valid
              Case ID and APP ID to load all associated data.
            </div>

            <div className="grid gap-3 sm:grid-cols-2">
              <div>
                <label htmlFor="select-case-id" className="label">
                  Case ID
                </label>
                <input
                  id="select-case-id"
                  name="caseId"
                  type="text"
                  autoComplete="off"
                  disabled={loading}
                  value={caseId}
                  onChange={(e) => setCaseId(e.target.value)}
                  placeholder="CASE-… or uuid"
                  className="input font-mono text-[13px]"
                  aria-invalid={Boolean(errorMessage && !caseId.trim())}
                />
              </div>
              <div>
                <label htmlFor="select-app-id" className="label">
                  APP ID
                </label>
                <input
                  id="select-app-id"
                  name="appId"
                  type="text"
                  autoComplete="off"
                  disabled={loading}
                  value={appId}
                  onChange={(e) => setAppId(e.target.value)}
                  placeholder="APP-… or uuid"
                  className="input font-mono text-[13px]"
                  aria-invalid={Boolean(errorMessage && !appId.trim())}
                />
              </div>
            </div>

            <button
              type="submit"
              disabled={loading || !caseId.trim() || !appId.trim()}
              className="btn btn-primary w-full justify-center"
            >
              {loading ? (
                <>
                  <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
                  <span>Loading case…</span>
                </>
              ) : (
                <>
                  <FolderOpen className="h-4 w-4" aria-hidden />
                  <span>Open case</span>
                </>
              )}
            </button>
          </form>

          {/* ── Divider ──────────────────────────────────────────── */}
          <div className="my-5 flex items-center gap-3">
            <span className="h-px flex-1 bg-line-divider" />
            <span className="text-[11px] font-medium uppercase tracking-widest text-content-disabled">
              or
            </span>
            <span className="h-px flex-1 bg-line-divider" />
          </div>

          {/* ── Create new case ──────────────────────────────────── */}
          <button
            type="button"
            onClick={onNewCase}
            className="btn btn-accent w-full justify-center"
          >
            <Plus className="h-4 w-4" aria-hidden />
            <span>Create new case</span>
          </button>
        </div>

        <p className="text-center text-[12px] text-content-secondary">
          Case ID and APP ID are not tied to your account — any valid combination can be accessed.
        </p>
      </div>
    </div>
  )
}
