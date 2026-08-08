/**
 * Chart colour assignment (`docs/UI.md` §11.2).
 *
 * The palette itself lives in `styles/tokens.css` as `--series-1` … `--series-8`; this module
 * owns the *assignment*, which is the part that goes wrong. Three rules, all binding:
 *
 * **Slots are assigned in fixed order and never cycled.** Colour follows the entity, not its
 * rank — a filter that removes a provider must not repaint the survivors. That is why
 * {@link providerSeries} is a lookup table rather than an index into a rotating array.
 *
 * **When the series *are* application states, use the status palette, not these slots.** A
 * stacked column of submitted / needs_review / failed is a *status* chart; categorical hues
 * there would make "failed" a random colour. {@link statusSeries} exists so that is a
 * one-word choice at the call site.
 *
 * **Never generate a ninth hue.** In an all-pairs form (scatter, small multiples) only the
 * first three slots are separable under colour-vision deficiency; past three, fold the tail
 * into "Other" or facet into small multiples. {@link seriesColor} clamps rather than wrapping,
 * so an overflowing series is visibly wrong instead of quietly ambiguous.
 */

import type { ApplicationStatus, ATSProviderName } from '@/lib/api/types';
import { statusTone } from '@/lib/utils';

/** Number of validated categorical slots. There is no ninth. */
export const SERIES_SLOT_COUNT = 8;

/**
 * Slots that stay separable in an **all-pairs** form (scatter, small multiples).
 *
 * Measured: the first three pass with worst-case CVD ΔE 9.4. Past three, the guarantee is
 * only for *adjacent* pairs, which is all a stacked bar or a line chart needs.
 */
export const SERIES_ALL_PAIRS_SAFE = 3;

/** CSS colour for a categorical slot, 1-indexed to match the token names. */
export function seriesColor(slot: number): string {
  const clamped = Math.min(Math.max(Math.trunc(slot), 1), SERIES_SLOT_COUNT);
  return `var(--series-${clamped})`;
}

/**
 * Provider → slot, permanently.
 *
 * Written once, here, exactly as §11.2 rule 2 specifies. Greenhouse, Lever and Ashby — the
 * three that support real submission — take the first three slots, which is also the set that
 * survives an all-pairs form.
 */
const PROVIDER_SLOTS: Readonly<Record<ATSProviderName, number>> = {
  greenhouse: 1,
  lever: 2,
  ashby: 3,
  workday: 4,
  manual: 6,
  linkedin: 7,
};

/** The permanent slot number for one provider. */
export function providerSlot(provider: ATSProviderName): number {
  return PROVIDER_SLOTS[provider];
}

/** The permanent colour for one provider. */
export function providerSeries(provider: ATSProviderName): string {
  return seriesColor(PROVIDER_SLOTS[provider]);
}

/** The status-family colour for one application state, for status-valued series. */
export function statusSeries(status: ApplicationStatus): string {
  return statusTone(status).color;
}

/**
 * The sequential ramp — one hue, the accent iris (§2.5).
 *
 * For histograms, heatmaps and density. Never a rainbow, and never the status palette: a
 * magnitude scale that borrows red and green makes a low value read as a failure.
 *
 * @param t - Position in the ramp, 0–1.
 */
export function sequentialColor(t: number): string {
  const step = Math.min(Math.max(Math.round(t * 4), 0), 4);
  return `var(--score-${step})`;
}

/**
 * The diverging pair, with a **grey** midpoint (§11.2 rule 5).
 *
 * For a delta against a target, or scores above and below `min_score`. A hue at the midpoint
 * would make "on target" look like a third category.
 */
export const DIVERGING = {
  negative: 'var(--series-8)',
  mid: 'var(--series-mid)',
  positive: 'var(--series-1)',
} as const;

/**
 * The emphasis treatment: one series in the accent, everything else receded.
 *
 * The most underused form in the document, and usually the honest answer to "make this chart
 * clearer" — it says *this one is different* in a way a full palette cannot.
 *
 * @param isSubject - Whether this mark is the one the story is about.
 */
export function emphasisColor(isSubject: boolean): string {
  return isSubject ? 'var(--accent)' : 'color-mix(in oklab, var(--fg-muted) 45%, transparent)';
}

/** Chart chrome, resolved to token references for a charting library's props. */
export const CHART_CHROME = {
  surface: 'var(--chart-surface)',
  grid: 'var(--chart-grid)',
  axis: 'var(--chart-axis)',
  ink: 'var(--chart-ink)',
  /** Gap between touching marks, in pixels. Painted in the surface colour, never a stroke. */
  surfaceGap: 2,
  /** Cap on bar/column thickness; leftover band width is air, not a fatter bar. */
  maxBarThickness: 24,
  /** Line weight, with round join and cap. */
  lineWidth: 2,
  /** Minimum marker diameter, so a point is a hit target as well as a mark. */
  markerSize: 8,
  /** Area-fill opacity — a wash, never a saturated block. */
  areaOpacity: 0.1,
} as const;
