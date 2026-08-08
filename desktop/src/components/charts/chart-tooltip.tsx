/**
 * The one tooltip surface every Recharts chart uses (`docs/UI.md` §11.4).
 *
 * Spec, restated because it is easy to lose in a library's defaults: `--bg-overlay`,
 * `--border-strong`, `--shadow-float`, `--radius-md`, 8px padding; a `--text-mini`
 * `--fg-secondary` header; rows of `[8px dot] label … mono value` with the values right
 * aligned and `tabular-nums`; and **no transition at all**, because the tooltip has to track
 * the pointer at pointer-move rate and a 140ms ease turns a sweep into a smear.
 *
 * Values wear `--fg-primary`, never the series colour: §11.2 rule 6 keeps colour on the mark
 * and the dot, so a colour-blind reader still gets the identity from the label beside it.
 */

import type { ReactNode } from 'react';

/** One row of the tooltip body. */
export interface TooltipRow {
  key: string;
  label: string;
  value: string;
  /** `var()` reference for the 8px key dot. Omit for a single-series chart. */
  color?: string;
}

/** Props for {@link TooltipSurface}. */
export interface TooltipSurfaceProps {
  heading?: ReactNode;
  rows: readonly TooltipRow[];
  /** A closing line — a total, a conversion rate. */
  footer?: ReactNode;
}

/** The panel itself, usable by the hand-rolled SVG charts as well as by Recharts. */
export function TooltipSurface({ heading, rows, footer }: TooltipSurfaceProps) {
  return (
    <div className="pointer-events-none min-w-[160px] rounded-md border border-strong bg-overlay p-2 shadow-float">
      {heading !== undefined && (
        <p className="mb-1 text-mini text-secondary">{heading}</p>
      )}
      <ul className="flex flex-col gap-0.5">
        {rows.map((row) => (
          <li key={row.key} className="flex min-h-[18px] items-center gap-2">
            {row.color !== undefined && (
              <span
                aria-hidden="true"
                className="size-2 shrink-0 rounded-full"
                style={{ backgroundColor: row.color }}
              />
            )}
            <span className="min-w-0 flex-1 truncate text-mini text-secondary">{row.label}</span>
            <span className="shrink-0 font-mono text-mini tabular-nums text-primary">
              {row.value}
            </span>
          </li>
        ))}
      </ul>
      {footer !== undefined && (
        <p className="mt-1 border-t border-state-divider pt-1 text-mini text-muted">{footer}</p>
      )}
    </div>
  );
}
