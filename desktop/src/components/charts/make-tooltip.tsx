/**
 * The Recharts `content` factory (`docs/UI.md` §11.4).
 *
 * Recharts hands its tooltip a payload of whatever series are live at that x. This turns that
 * into the product's tooltip surface, with two rules baked in so no chart has to remember
 * them:
 *
 * **Zero-valued series are dropped.** A stacked column with four series and one non-zero
 * segment should not list three zeroes — the tooltip is there to say what happened, not to
 * enumerate what did not.
 *
 * **Values wear `--fg-primary`, never the series colour** (§11.2 rule 6). Colour lives on the
 * 8px key dot beside the label, so a colour-blind reader gets identity from the label and the
 * number stays maximally legible.
 */

import type { ReactNode } from 'react';
import type { TooltipProps } from 'recharts';

import { TooltipSurface, type TooltipRow } from './chart-tooltip';

/**
 * Build a Recharts `content` renderer.
 *
 * @param formatHeading - Turns the category value into the tooltip's header line.
 * @param formatValue - Renders one datum. Defaults to a grouped integer.
 * @param hideZeros - Drop series contributing nothing at this x.
 */
export function makeTooltip(
  formatHeading: (label: unknown) => string,
  formatValue: (value: number) => string = (value) => value.toLocaleString(),
  hideZeros = true,
): (props: TooltipProps<number, string>) => ReactNode {
  return function RenderTooltip(props: TooltipProps<number, string>): ReactNode {
    if (props.active !== true) return null;
    const payload = props.payload ?? [];

    const rows: TooltipRow[] = [];
    for (const [index, entry] of payload.entries()) {
      const value = typeof entry.value === 'number' ? entry.value : 0;
      if (hideZeros && value === 0) continue;
      rows.push({
        key: String(entry.dataKey ?? index),
        label: String(entry.name ?? entry.dataKey ?? ''),
        value: formatValue(value),
        ...(entry.color === undefined ? {} : { color: entry.color }),
      });
    }

    if (rows.length === 0) return null;
    return <TooltipSurface heading={formatHeading(props.label)} rows={rows} />;
  };
}
