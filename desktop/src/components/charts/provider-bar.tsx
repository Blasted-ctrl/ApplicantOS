/**
 * Applications by ATS provider (`docs/UI.md` §11.1 — "which provider produces outcomes?").
 *
 * Horizontal bars, one row per provider, in the **permanent** provider slots from §11.2 rule
 * 2: greenhouse is always slot 1, lever always slot 2, ashby always slot 3. Colour follows the
 * entity rather than its rank, so filtering one provider out never repaints the survivors —
 * the failure mode that makes a dashboard impossible to read twice.
 *
 * Rows are sorted by volume because that is the question being asked, and the colour is what
 * keeps identity stable while the order moves.
 */

import { providerSeries } from '@/lib/chart/series';
import { cn, formatNumber, formatPercent, providerLabel } from '@/lib/utils';

import type { ProviderDatum } from './data';

/** Props for {@link ProviderBar}. */
export interface ProviderBarProps {
  data: readonly ProviderDatum[];
  className?: string;
}

/** Horizontal volume bars, one per provider. */
export function ProviderBar({ data, className }: ProviderBarProps) {
  const total = data.reduce((sum, datum) => sum + datum.count, 0);
  const widest = data.reduce((largest, datum) => Math.max(largest, datum.count), 0);

  return (
    <ol className={cn('flex flex-col gap-1.5', className)}>
      {data.map((datum) => (
        <li key={datum.provider} className="flex items-center gap-3">
          <span className="inline-flex w-[104px] shrink-0 items-center gap-1.5 truncate text-sm text-secondary">
            <span
              aria-hidden="true"
              className="size-2 shrink-0 rounded-full"
              style={{ backgroundColor: providerSeries(datum.provider) }}
            />
            {providerLabel(datum.provider)}
          </span>
          <span className="min-w-0 flex-1">
            <span
              className="block rounded-r-xs"
              style={{
                height: 20,
                width: `${String(widest === 0 ? 0 : Math.max((datum.count / widest) * 100, datum.count > 0 ? 1.5 : 0))}%`,
                backgroundColor: providerSeries(datum.provider),
              }}
            />
          </span>
          <span className="w-[56px] shrink-0 text-right font-mono text-mini tabular-nums text-primary">
            {formatNumber(datum.count)}
          </span>
          <span className="w-[48px] shrink-0 text-right font-mono text-micro tracking-normal text-muted">
            {formatPercent(total === 0 ? 0 : datum.count / total)}
          </span>
        </li>
      ))}
    </ol>
  );
}
