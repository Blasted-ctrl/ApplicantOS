/**
 * Formatting and mapping helpers shared by every component.
 *
 * Two rules from `docs/UI.md` are enforced here rather than left to call sites, because a
 * rule that lives in a code review does not survive contact with forty components:
 *
 * **Unknown is an em-dash.** `docs/UI.md` §3.7 forbids `N/A`, `null`, `None`, and an empty
 * cell. {@link orDash} is the only correct way to render a value that might be absent, and
 * it is what makes a sparse table look designed rather than broken.
 *
 * **Colour is never the only channel.** Every mapping in the status and score sections
 * returns a label and a shape alongside its colour token (§12.2). A component that destructures
 * only `color` is visibly wrong on review, which is the point.
 */

import { clsx, type ClassValue } from 'clsx';
import { extendTailwindMerge } from 'tailwind-merge';

import {
  APPLICATION_IN_FLIGHT_STATES,
  type ApplicationStatus,
  type ATSProviderName,
  type CheckpointStatus,
  type IndexStatus,
  type PostingStatus,
  type ReviewReason,
  type SessionStatus,
  type SignalKind,
  type SourceKind,
} from '@/lib/api/types';

// ══════════════════════════════════════════════════════════════════════════════════════
// Class names
// ══════════════════════════════════════════════════════════════════════════════════════

/**
 * `tailwind-merge` taught our theme.
 *
 * The default configuration knows Tailwind's stock scales; ours replaced them wholesale
 * (`--color-*: initial`, `--text-*: initial`, `--radius-*: initial`). Without this,
 * `twMerge('text-muted', 'text-primary')` would treat both as unknown classes, keep both,
 * and leave the winner to CSS source order — which is exactly the class of bug that makes a
 * conditional colour "not apply" for no visible reason.
 */
const TOKEN_COLORS = [
  'transparent',
  'current',
  'inherit',
  'white',
  'black',
  'chrome',
  'page',
  'surface',
  'elevated',
  'overlay',
  'inset',
  'subtle',
  'default',
  'strong',
  'primary',
  'secondary',
  'muted',
  'disabled',
  'on-accent',
  'on-status',
  'accent',
  'accent-hover',
  'accent-active',
  'accent-text',
  'accent-subtle',
  'accent-wash',
  'accent-border',
  'state-hover',
  'state-selected',
  'state-pressed',
  'state-divider',
  'state-track',
  'focus-ring',
  'st-neutral',
  'st-dim',
  'st-progress',
  'st-success',
  'st-review',
  'st-danger',
  'st-rejected',
  'st-interview',
  'st-offer',
  'score-0',
  'score-1',
  'score-2',
  'score-3',
  'score-4',
  'score-threshold',
  'chart-surface',
  'chart-grid',
  'chart-axis',
  'chart-ink',
  'series-1',
  'series-2',
  'series-3',
  'series-4',
  'series-5',
  'series-6',
  'series-7',
  'series-8',
  'series-mid',
] as const;

const TOKEN_TEXT_SIZES = [
  'micro',
  'mini',
  'sm',
  'base',
  'md',
  'lg',
  'xl',
  '2xl',
  'hero',
] as const;

const twMerge = extendTailwindMerge({
  override: {
    classGroups: {
      'font-size': [{ text: [...TOKEN_TEXT_SIZES] }],
      'text-color': [{ text: [...TOKEN_COLORS] }],
      'bg-color': [{ bg: [...TOKEN_COLORS] }],
      'border-color': [{ border: [...TOKEN_COLORS] }],
      'border-color-t': [{ 'border-t': [...TOKEN_COLORS] }],
      'border-color-r': [{ 'border-r': [...TOKEN_COLORS] }],
      'border-color-b': [{ 'border-b': [...TOKEN_COLORS] }],
      'border-color-l': [{ 'border-l': [...TOKEN_COLORS] }],
      'ring-color': [{ ring: [...TOKEN_COLORS] }],
      'outline-color': [{ outline: [...TOKEN_COLORS] }],
      'font-weight': [{ font: ['normal', 'medium', 'semibold', 'bold'] }],
      rounded: [{ rounded: ['none', 'xs', 'sm', 'md', 'lg', 'xl', 'full'] }],
      'shadow-color': [],
      shadow: [{ shadow: ['none', 'raised', 'float', 'dialog', 'accent', 'selected'] }],
      ease: [{ ease: ['out', 'out-quad', 'in-out', 'drawer', 'exit', 'initial', 'linear'] }],
    },
  },
});

/** Merge class names, with later Tailwind utilities winning over earlier conflicting ones. */
export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}

// ══════════════════════════════════════════════════════════════════════════════════════
// Text and numbers
// ══════════════════════════════════════════════════════════════════════════════════════

/** The one placeholder for an unknown value (`docs/UI.md` §3.7). */
export const EM_DASH = '—';

/** Render `value`, or an em-dash when it is absent or empty. Never `N/A`, `null`, or blank. */
export function orDash(value: string | number | null | undefined): string {
  if (value === null || value === undefined) return EM_DASH;
  if (typeof value === 'number') return Number.isFinite(value) ? String(value) : EM_DASH;
  return value.trim() === '' ? EM_DASH : value;
}

/** Group a number for display. Table cells never compact; use {@link compactNumber} in tiles. */
export function formatNumber(value: number): string {
  return new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 }).format(value);
}

/**
 * Compact a number for a stat tile: `1,284` stays, `12,900` becomes `12.9K` (§3.7).
 *
 * The threshold is 10,000 rather than 1,000 because four digits still read instantly and
 * `1.3K` throws away information the user came to the tile for.
 */
export function compactNumber(value: number): string {
  if (!Number.isFinite(value)) return EM_DASH;
  if (Math.abs(value) < 10_000) return formatNumber(value);
  return new Intl.NumberFormat(undefined, {
    notation: 'compact',
    maximumFractionDigits: 1,
  })
    .format(value)
    .toUpperCase();
}

/**
 * Format a 0–1 ratio as a percentage.
 *
 * @param fraction - A ratio, not a percentage. `0.34` renders as `34%`.
 */
export function formatPercent(fraction: number | null | undefined, digits = 0): string {
  if (fraction === null || fraction === undefined || !Number.isFinite(fraction)) return EM_DASH;
  return `${(fraction * 100).toFixed(digits)}%`;
}

/**
 * Format a duration in seconds compactly: `48s`, `2m 12s`, `1h 04m`.
 *
 * Durations are mono (§3.5), and the two-part form is capped at two units so a column never
 * changes width when a run crosses an hour.
 */
export function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return EM_DASH;
  const total = Math.max(0, Math.round(seconds));
  if (total < 60) return `${total}s`;
  const minutes = Math.floor(total / 60);
  if (minutes < 60) return `${minutes}m ${String(total % 60).padStart(2, '0')}s`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ${String(minutes % 60).padStart(2, '0')}m`;
  return `${Math.floor(hours / 24)}d ${String(hours % 24).padStart(2, '0')}h`;
}

/** Bytes as `1.4 MB`. Used for artifact sizes. */
export function formatBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined || !Number.isFinite(bytes)) return EM_DASH;
  if (bytes < 1024) return `${bytes} B`;
  const units = ['KB', 'MB', 'GB', 'TB'] as const;
  let value = bytes / 1024;
  let unitIndex = 0;
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024;
    unitIndex += 1;
  }
  return `${value.toFixed(value < 10 ? 1 : 0)} ${units[unitIndex] ?? 'TB'}`;
}

// ══════════════════════════════════════════════════════════════════════════════════════
// Dates
// ══════════════════════════════════════════════════════════════════════════════════════

/** Parse an ISO string to a `Date`, or `null` when it is absent or unparseable. */
export function toDate(value: string | number | Date | null | undefined): Date | null {
  if (value === null || value === undefined || value === '') return null;
  const date = value instanceof Date ? value : new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

/** `Aug 8, 2026`. */
export function formatDate(value: string | number | Date | null | undefined): string {
  const date = toDate(value);
  if (date === null) return EM_DASH;
  return new Intl.DateTimeFormat(undefined, {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
  }).format(date);
}

/** `Aug 8, 2026, 09:41`. */
export function formatDateTime(value: string | number | Date | null | undefined): string {
  const date = toDate(value);
  if (date === null) return EM_DASH;
  return new Intl.DateTimeFormat(undefined, {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).format(date);
}

/** `09:41:02`. The log-line and timeline time column. */
export function formatTime(value: string | number | Date | null | undefined): string {
  const date = toDate(value);
  if (date === null) return EM_DASH;
  return new Intl.DateTimeFormat(undefined, {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  }).format(date);
}

/**
 * The full ISO-8601 timestamp, for the `title` attribute and tooltips.
 *
 * §3.7: relative time in the cell, absolute ISO in the tooltip. Both, always — the relative
 * form is for scanning and the absolute form is for the one time the user needs certainty.
 */
export function formatIso(value: string | number | Date | null | undefined): string {
  const date = toDate(value);
  return date === null ? EM_DASH : date.toISOString();
}

const MINUTE_MS = 60_000;
const HOUR_MS = 60 * MINUTE_MS;
const DAY_MS = 24 * HOUR_MS;
const WEEK_MS = 7 * DAY_MS;

/**
 * Compact relative time: `now`, `12s ago`, `4m ago`, `3h ago`, `2d ago`, `5w ago`.
 *
 * Deliberately not `Intl.RelativeTimeFormat`, whose output ("2 days ago") is three times
 * wider and would force the column to size for the longest string. Past a year it falls back
 * to an absolute date, because "63w ago" is not a fact anyone can use.
 *
 * @param value - The timestamp to describe.
 * @param now - Reference point; injectable so the formatting is testable.
 */
export function formatRelative(
  value: string | number | Date | null | undefined,
  now: Date = new Date(),
): string {
  const date = toDate(value);
  if (date === null) return EM_DASH;

  const deltaMs = now.getTime() - date.getTime();
  const future = deltaMs < 0;
  const abs = Math.abs(deltaMs);
  const suffix = future ? 'from now' : 'ago';

  if (abs < 5_000) return 'now';
  if (abs < MINUTE_MS) return `${Math.floor(abs / 1000)}s ${suffix}`;
  if (abs < HOUR_MS) return `${Math.floor(abs / MINUTE_MS)}m ${suffix}`;
  if (abs < DAY_MS) return `${Math.floor(abs / HOUR_MS)}h ${suffix}`;
  if (abs < WEEK_MS) return `${Math.floor(abs / DAY_MS)}d ${suffix}`;
  if (abs < 52 * WEEK_MS) return `${Math.floor(abs / WEEK_MS)}w ${suffix}`;
  return formatDate(date);
}

// ══════════════════════════════════════════════════════════════════════════════════════
// Scores — docs/UI.md §2.5
// ══════════════════════════════════════════════════════════════════════════════════════

/** Ordinal band of a 0–100 score. */
export type ScoreBand = 0 | 1 | 2 | 3 | 4;

/** The five band boundaries, as `[inclusive lower bound, band]`, highest first. */
const SCORE_BANDS: readonly (readonly [number, ScoreBand])[] = [
  [85, 4],
  [70, 3],
  [60, 2],
  [40, 1],
  [0, 0],
];

/** Which band a normalised score falls in. */
export function scoreBand(normalized: number): ScoreBand {
  const clamped = clamp(normalized, 0, 100);
  for (const [lower, band] of SCORE_BANDS) {
    if (clamped >= lower) return band;
  }
  return 0;
}

/**
 * The CSS colour for a score, as a `var()` reference.
 *
 * A one-hue ordinal ramp in the accent, deliberately *not* red→green: score is a magnitude,
 * and keeping green/amber/red meaning *outcome* rather than *quality* is what stops a 62 from
 * reading as a failure.
 */
export function scoreColor(normalized: number): string {
  return `var(--score-${scoreBand(normalized)})`;
}

/** The verdict word for a score band. Rendered beside the bar so colour is not the only channel. */
export function scoreVerdict(normalized: number): string {
  const words = ['Reject', 'Weak', 'Borderline', 'Apply', 'Strong'] as const;
  return words[scoreBand(normalized)] ?? 'Reject';
}

// ══════════════════════════════════════════════════════════════════════════════════════
// Status — docs/UI.md §2.4
// ══════════════════════════════════════════════════════════════════════════════════════

/** The nine semantic colour families. Every status in the product maps onto one of these. */
export type StatusFamily =
  | 'neutral'
  | 'dim'
  | 'progress'
  | 'success'
  | 'review'
  | 'danger'
  | 'rejected'
  | 'interview'
  | 'offer';

/** StatusDot shape. Hollow means "waiting on them"; filled means "settled"; dashed means dead. */
export type DotShape = 'filled' | 'ring' | 'dashed';

/** Everything a badge or dot needs to render one status, in all three channels. */
export interface StatusTone {
  /** The semantic family, which is the colour token's name. */
  family: StatusFamily;
  /** Human label. Always rendered — a status colour never appears without one (§2.7 rule 3). */
  label: string;
  /** Dot shape, the second channel. */
  dot: DotShape;
  /** Whether the dot pulses. True only for non-terminal states (§2.4 rule 1). */
  animated: boolean;
  /** `var(--st-…)` — the family colour, valid as a fill, a border, and a dot. */
  color: string;
  /**
   * `var(…)` for the *label text*.
   *
   * Equal to {@link color} except for the `dim` family: `--st-dim` measures 3.07:1 and is
   * legal as a mark but not as text (§2.6), so `abandoned` and `ghosted` wear `--fg-muted`
   * and let the dot carry the tone.
   */
  text: string;
  /** Badge background alpha over {@link color}, per §7.6. Ignored when {@link solid}. */
  wash: number;
  /** True only for `offer` — the one status allowed to be as loud as the primary action. */
  solid: boolean;
}

function tone(
  family: StatusFamily,
  label: string,
  dot: DotShape,
  animated: boolean,
  wash: number,
  solid = false,
): StatusTone {
  const color = `var(--st-${family})`;
  return {
    family,
    label,
    dot,
    animated,
    color,
    text: family === 'dim' ? 'var(--fg-muted)' : color,
    wash,
    solid,
  };
}

/** The binding `ApplicationStatus` → visual mapping. All thirteen values (§2.4). */
const APPLICATION_TONES: Readonly<Record<ApplicationStatus, StatusTone>> = {
  draft: tone('neutral', 'Draft', 'ring', false, 0.12),
  preparing: tone('progress', 'Preparing', 'filled', true, 0.12),
  ready: tone('progress', 'Ready', 'ring', false, 0.12),
  submitting: tone('progress', 'Submitting', 'filled', true, 0.12),
  submitted: tone('success', 'Submitted', 'ring', false, 0.12),
  confirmed: tone('success', 'Confirmed', 'filled', false, 0.12),
  needs_review: tone('review', 'Needs review', 'filled', true, 0.14),
  failed: tone('danger', 'Failed', 'filled', false, 0.14),
  abandoned: tone('dim', 'Abandoned', 'dashed', false, 0.1),
  rejected: tone('rejected', 'Rejected', 'filled', false, 0.12),
  interview: tone('interview', 'Interview', 'filled', false, 0.14),
  offer: tone('offer', 'Offer', 'filled', false, 1, true),
  ghosted: tone('dim', 'Ghosted', 'dashed', false, 0.1),
};

/** The visual treatment for one application status. */
export function statusTone(status: ApplicationStatus): StatusTone {
  return APPLICATION_TONES[status];
}

/** The human label for one application status. */
export function statusLabel(status: ApplicationStatus): string {
  return APPLICATION_TONES[status].label;
}

/** Whether this status animates its dot. Equivalent to membership of the in-flight set. */
export function isInFlight(status: ApplicationStatus): boolean {
  return APPLICATION_IN_FLIGHT_STATES.has(status);
}

/**
 * `PostingStatus` → the same nine families, **badge only**.
 *
 * §2.4: the animated dot is reserved for the application lifecycle. Everything else gets a
 * Badge, so every tone below has `animated: false`.
 */
const POSTING_TONES: Readonly<Record<PostingStatus, StatusTone>> = {
  discovered: tone('neutral', 'Discovered', 'ring', false, 0.12),
  deduped: tone('neutral', 'Deduped', 'ring', false, 0.12),
  scored: tone('progress', 'Scored', 'ring', false, 0.12),
  queued: tone('progress', 'Queued', 'ring', false, 0.12),
  processing: tone('progress', 'Processing', 'filled', false, 0.12),
  applied: tone('success', 'Applied', 'filled', false, 0.12),
  skipped: tone('dim', 'Skipped', 'dashed', false, 0.1),
  needs_review: tone('review', 'Needs review', 'filled', false, 0.14),
  failed: tone('danger', 'Failed', 'filled', false, 0.14),
  expired: tone('dim', 'Expired', 'dashed', false, 0.1),
};

/** The visual treatment for one posting status. */
export function postingStatusTone(status: PostingStatus): StatusTone {
  return POSTING_TONES[status];
}

/** `SessionStatus` → family tokens. Badge only. */
const SESSION_TONES: Readonly<Record<SessionStatus, StatusTone>> = {
  running: tone('progress', 'Running', 'filled', false, 0.12),
  completed: tone('success', 'Completed', 'filled', false, 0.12),
  failed: tone('danger', 'Failed', 'filled', false, 0.14),
  cancelled: tone('danger', 'Cancelled', 'ring', false, 0.14),
};

/** The visual treatment for one run session's status. */
export function sessionStatusTone(status: SessionStatus): StatusTone {
  return SESSION_TONES[status];
}

/** `IndexStatus` → family tokens. Badge only. */
const INDEX_TONES: Readonly<Record<IndexStatus, StatusTone>> = {
  pending: tone('neutral', 'Pending', 'ring', false, 0.12),
  indexing: tone('progress', 'Indexing', 'filled', false, 0.12),
  indexed: tone('success', 'Indexed', 'filled', false, 0.12),
  failed: tone('danger', 'Failed', 'filled', false, 0.14),
  skipped: tone('dim', 'Skipped', 'dashed', false, 0.1),
  stale: tone('review', 'Stale', 'ring', false, 0.14),
};

/** The visual treatment for one knowledge source's index status. */
export function indexStatusTone(status: IndexStatus): StatusTone {
  return INDEX_TONES[status];
}

/** `CheckpointStatus` → family tokens. Badge only. */
const CHECKPOINT_TONES: Readonly<Record<CheckpointStatus, StatusTone>> = {
  pending: tone('neutral', 'Pending', 'ring', false, 0.12),
  running: tone('progress', 'Running', 'filled', false, 0.12),
  succeeded: tone('success', 'Succeeded', 'filled', false, 0.12),
  failed: tone('danger', 'Failed', 'filled', false, 0.14),
  compensated: tone('dim', 'Compensated', 'dashed', false, 0.1),
};

/** The visual treatment for one checkpoint's status. */
export function checkpointStatusTone(status: CheckpointStatus): StatusTone {
  return CHECKPOINT_TONES[status];
}

/** `SignalKind` → family tokens, for the status-sync panel. */
const SIGNAL_TONES: Readonly<Record<SignalKind, StatusTone>> = {
  application_received: tone('success', 'Received', 'ring', false, 0.12),
  viewed: tone('neutral', 'Viewed', 'ring', false, 0.12),
  assessment_requested: tone('review', 'Assessment', 'filled', false, 0.14),
  interview_invite: tone('interview', 'Interview invite', 'filled', false, 0.14),
  offer: tone('offer', 'Offer', 'filled', false, 1, true),
  rejection: tone('rejected', 'Rejection', 'filled', false, 0.12),
  withdrawn: tone('dim', 'Withdrawn', 'dashed', false, 0.1),
  ghosted_inferred: tone('dim', 'Ghosted', 'dashed', false, 0.1),
  unknown: tone('neutral', 'Unclassified', 'ring', false, 0.12),
};

/** The visual treatment for one status signal's kind. */
export function signalKindTone(kind: SignalKind): StatusTone {
  return SIGNAL_TONES[kind];
}

// ══════════════════════════════════════════════════════════════════════════════════════
// Labels for enum values
// ══════════════════════════════════════════════════════════════════════════════════════

/** Display names for ATS providers. Product names, so their capitalisation is theirs. */
const PROVIDER_LABELS: Readonly<Record<ATSProviderName, string>> = {
  linkedin: 'LinkedIn',
  greenhouse: 'Greenhouse',
  lever: 'Lever',
  ashby: 'Ashby',
  workday: 'Workday',
  manual: 'Manual',
};

/** The display name of an ATS provider. */
export function providerLabel(provider: ATSProviderName): string {
  return PROVIDER_LABELS[provider];
}

/**
 * Why an application is in the review queue, in the user's words.
 *
 * Each of these is a refusal to fabricate, and the copy says what the human has to do —
 * "the form asked something the agent could not answer", not "unknown_field".
 */
const REVIEW_REASON_LABELS: Readonly<Record<ReviewReason, string>> = {
  too_many_essays: 'Too many essay questions',
  unknown_field: 'Unrecognised form field',
  login_required: 'Sign-in required',
  captcha: 'CAPTCHA challenge',
  mfa: 'Two-factor prompt',
  file_upload_failed: 'File upload failed',
  ambiguous_answer: 'Ambiguous answer',
  low_confidence: 'Low answer confidence',
  policy_block: 'Blocked by your rules',
  submit_not_found: 'Submit button not found',
  unsupported_flow: 'Flow not automatable',
  verification_failed: 'Submission unverified',
  rate_limited: 'Provider rate limit',
  insufficient_knowledge: 'Not enough indexed knowledge',
};

/** The display label for a review reason. */
export function reviewReasonLabel(reason: ReviewReason): string {
  return REVIEW_REASON_LABELS[reason];
}

/** Display names for knowledge source kinds. */
const SOURCE_KIND_LABELS: Readonly<Record<SourceKind, string>> = {
  github_repo: 'GitHub repository',
  github_profile: 'GitHub profile',
  portfolio_page: 'Portfolio page',
  personal_website: 'Personal website',
  project_folder: 'Project folder',
  resume: 'Résumé',
  cover_letter: 'Cover letter',
  readme: 'README',
  documentation: 'Documentation',
  linkedin_export: 'LinkedIn export',
  interview_note: 'Interview note',
  user_correction: 'Your correction',
  generated_resume: 'Generated résumé',
  blog_post: 'Blog post',
  video: 'Video',
  manual_entry: 'Manual entry',
};

/** The display label for a knowledge source kind. */
export function sourceKindLabel(kind: SourceKind): string {
  return SOURCE_KIND_LABELS[kind];
}

/**
 * Turn a snake_case enum value into a readable label.
 *
 * The fallback for the handful of free-string fields the API carries (`ApplicationEvent.kind`,
 * `RunSession.trigger`) that have no closed vocabulary to write labels for.
 */
export function humanize(value: string): string {
  const spaced = value.replace(/[_-]+/g, ' ').trim();
  if (spaced === '') return EM_DASH;
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}

// ══════════════════════════════════════════════════════════════════════════════════════
// Deltas — docs/UI.md §7.9
// ══════════════════════════════════════════════════════════════════════════════════════

/** A stat-tile delta, resolved into all three channels. */
export interface DeltaTone {
  /** `▲`, `▼`, or an em-dash when the delta is zero or unknown. */
  glyph: string;
  /** `var(--st-…)` or `var(--fg-muted)`. */
  color: string;
  /** `+12%`, `−4%`, or an em-dash. */
  label: string;
  direction: 'up' | 'down' | 'flat';
}

/**
 * Colour and glyph for a delta.
 *
 * The colour is **direction × whether up is good**, never green-for-up unconditionally:
 * submissions rising is `--st-success`, failures rising is `--st-danger`, and getting that
 * backwards is the single most common dashboard lie.
 *
 * @param delta - Signed change, as a ratio (`0.12` renders as `+12%`).
 * @param upIsGood - Whether an increase is a good outcome for this metric.
 */
export function deltaTone(
  delta: number | null | undefined,
  upIsGood = true,
): DeltaTone {
  if (delta === null || delta === undefined || !Number.isFinite(delta) || delta === 0) {
    return { glyph: EM_DASH, color: 'var(--fg-muted)', label: EM_DASH, direction: 'flat' };
  }
  const up = delta > 0;
  const good = up === upIsGood;
  return {
    glyph: up ? '▲' : '▼',
    color: good ? 'var(--st-success)' : 'var(--st-danger)',
    // U+2212 MINUS SIGN, not a hyphen: it matches the digit width in every mono face.
    label: `${up ? '+' : '−'}${Math.abs(delta * 100).toFixed(0)}%`,
    direction: up ? 'up' : 'down',
  };
}

// ══════════════════════════════════════════════════════════════════════════════════════
// Small general helpers
// ══════════════════════════════════════════════════════════════════════════════════════

/** Constrain `value` to `[min, max]`. */
export function clamp(value: number, min: number, max: number): number {
  return Math.min(Math.max(value, min), max);
}

/**
 * Whether an empty state may render (`docs/UI.md` §7.17, binding).
 *
 * While a query has never had data *and* the user has not typed anything, render nothing at
 * all. The shell already occupies the space, and a "No results" that appears for 200ms and is
 * then replaced by fifty rows is the single most common reason a fast app feels broken.
 *
 * Lives here rather than beside `EmptyState` so every list screen reaches the same predicate,
 * and so the rule is one function rather than one condition repeated six times.
 *
 * @param isEmpty - Whether the fetched page has zero rows.
 * @param isPending - The query's `isPending`. Never `isFetching` — §10.1 makes `isPending` the
 *   only value permitted to gate content.
 * @param query - The current search string.
 */
export function shouldShowEmptyState(
  isEmpty: boolean,
  isPending: boolean,
  query: string,
): boolean {
  if (!isEmpty) return false;
  if (isPending && query === '') return false;
  return true;
}

/**
 * Whether an event target is a text-entry surface.
 *
 * Keyboard shortcuts are suspended while a text field has focus, except `Esc`, `⌘K` and
 * `⌘↵` (§12.3). This is the predicate that decides.
 */
export function isTypingTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  if (target.isContentEditable) return true;
  const tag = target.tagName;
  if (tag === 'TEXTAREA' || tag === 'SELECT') return true;
  if (tag !== 'INPUT') return false;
  const type = (target as HTMLInputElement).type;
  // Checkboxes, radios and buttons are inputs but not text entry, so shortcuts still apply.
  return !['checkbox', 'radio', 'button', 'submit', 'reset', 'range', 'color'].includes(type);
}

/** The platform's modifier key, used for both key handling and glyph rendering. */
export function isApplePlatform(): boolean {
  if (typeof navigator === 'undefined') return false;
  return /mac|iphone|ipad|ipod/i.test(navigator.platform || navigator.userAgent);
}

/** A stable, readable id fragment — the first eight characters of a UUID, for display only. */
export function shortId(id: string): string {
  return id.length <= 8 ? id : `${id.slice(0, 8)}…`;
}
