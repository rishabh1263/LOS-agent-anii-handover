/**
 * Fuzzy name matching for profile vs extracted document values.
 * Handles case, spacing, fused names, middle names, and minor OCR/spelling drift.
 *
 * Examples that should match:
 * - "Rishabh singh" vs "RISHABHAJITSINGH"
 * - "R. Singh" vs "Rishabh Singh"
 * - "Kumar" vs "Suresh Kumar"
 */

/** Lowercase alphanumeric only */
function collapse(s: string): string {
  return s.toLowerCase().replace(/[^a-z0-9]/g, '')
}

/** Word tokens (length > 1), lowercased */
function tokens(s: string): string[] {
  return s
    .toLowerCase()
    .replace(/[^a-z0-9\s.]/g, ' ')
    .split(/[\s.]+/)
    .filter((t) => t.length > 0)
}

function bigrams(s: string): Set<string> {
  const set = new Set<string>()
  if (s.length < 2) {
    if (s) set.add(s)
    return set
  }
  for (let i = 0; i < s.length - 1; i++) set.add(s.slice(i, i + 2))
  return set
}

function bigramJaccard(a: string, b: string): number {
  const A = bigrams(a)
  const B = bigrams(b)
  if (A.size === 0 || B.size === 0) return 0
  let inter = 0
  for (const g of A) if (B.has(g)) inter++
  return inter / (A.size + B.size - inter)
}

/** Classic Levenshtein distance */
function levenshtein(a: string, b: string): number {
  const m = a.length
  const n = b.length
  if (m === 0) return n
  if (n === 0) return m
  const row = new Array<number>(n + 1)
  for (let j = 0; j <= n; j++) row[j] = j
  for (let i = 1; i <= m; i++) {
    let prev = row[0]
    row[0] = i
    for (let j = 1; j <= n; j++) {
      const tmp = row[j]
      const cost = a[i - 1] === b[j - 1] ? 0 : 1
      row[j] = Math.min(row[j] + 1, row[j - 1] + 1, prev + cost)
      prev = tmp
    }
  }
  return row[n]
}

/** Similarity in [0, 1] from Levenshtein */
function editSimilarity(a: string, b: string): number {
  if (!a && !b) return 1
  if (!a || !b) return 0
  const dist = levenshtein(a, b)
  return 1 - dist / Math.max(a.length, b.length)
}

/**
 * True when token `t` is explained by string `hay` (substring or close edit).
 * Single-letter tokens match as initials (first char of a word in hay).
 */
function tokenExplainedBy(t: string, hayCollapsed: string, hayTokens: string[]): boolean {
  if (t.length === 1) {
    return hayTokens.some((w) => w[0] === t) || hayCollapsed.includes(t)
  }
  if (hayCollapsed.includes(t)) return true
  // Allow small OCR/spelling drift on longer tokens
  for (const w of hayTokens) {
    if (w.includes(t) || t.includes(w)) return true
    if (t.length >= 4 && w.length >= 4 && editSimilarity(t, w) >= 0.8) return true
  }
  return false
}

/**
 * Fraction of `need` tokens explained by `hay` name.
 */
function tokenCoverage(need: string[], hay: string): number {
  if (need.length === 0) return 0
  const hayC = collapse(hay)
  const hayT = tokens(hay)
  let hit = 0
  for (const t of need) {
    if (tokenExplainedBy(t, hayC, hayT)) hit++
  }
  return hit / need.length
}

/**
 * Fuzzy score in [0, 1]. Higher = closer match.
 * Use namesMatch() for a boolean gate at the default threshold.
 */
export function nameMatchScore(entered: string, extracted: string): number {
  const e = entered.trim()
  const x = extracted.trim()
  if (!e || !x) return 0

  const ce = collapse(e)
  const cx = collapse(x)
  if (!ce || !cx) return 0

  // Exact / containment on collapsed forms
  if (ce === cx) return 1
  if (ce.includes(cx) || cx.includes(ce)) return 0.95

  const te = tokens(e)
  const tx = tokens(x)

  // All significant entered tokens found in extracted (e.g. Rishabh + singh inside RISHABHAJITSINGH)
  const covEx = tokenCoverage(
    te.filter((t) => t.length > 1),
    x,
  )
  const covEn = tokenCoverage(
    tx.filter((t) => t.length > 1),
    e,
  )
  const tokenScore = Math.max(covEx, covEn)

  // Character-level signals for fused or reordered names
  const jaccard = bigramJaccard(ce, cx)
  const edit = editSimilarity(ce, cx)

  // Weighted blend; token coverage dominates for multi-part Indian names
  const score = Math.max(tokenScore * 0.7 + jaccard * 0.2 + edit * 0.1, jaccard, edit * 0.9)

  return Math.min(1, Math.max(0, score))
}

/** Default threshold for treating two names as the same person */
export const NAME_MATCH_THRESHOLD = 0.55

/**
 * Returns true when entered name is consistent with extracted name.
 */
export function namesMatch(
  entered: string,
  extracted: string,
  threshold: number = NAME_MATCH_THRESHOLD,
): boolean {
  return nameMatchScore(entered, extracted) >= threshold
}
