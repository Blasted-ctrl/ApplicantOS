/**
 * The emphasis bar (`docs/UI.md` §11.1) — one series in the accent, everything else receded.
 *
 * §11.1 calls it the most underused form in the document and usually the honest answer to
 * "make this chart clearer", because it says *this one is different* in a way a full palette
 * cannot. It is the right form for "which score band actually gets interviews": the reader is
 * not comparing five categories, they are looking for the one that stands out.
 *
 * **The sample size travels with the bar.** A 100% interview rate over two applications is not
 * a finding, and a chart that renders it at full width without saying `n = 2` is the exact
 * failure the product's insight rules exist to prevent. Bars below the confidence floor are
 * drawn in the receded tone and labelled with their n, whatever their value.
 */

import type { ReactNode } from 'react';

import { emphasisColor } from '@/lib/chart/series';
import { cn, formatPercent } from '@/lib/utils';

import { MIN_CONFIDENT_SAMPLE, type EmphasisDatum } from './data';

/** Props for {@link EmphasisBar}. */
export interface EmphasisBarProps {
  data: readonly EmphasisDatum[];
  /** A closing line under the bars — the caveat, the definition. */
  footnote?: ReactNode;
  className?: string;
}

/** Horizontal bars with exactly one subject. */
export function EmphasisBar({ data, footnote, className }: EmphasisBarProps) {
  // The subject is the best-performing band that has enough observations to mean anything.
  const confident = data.filter((datum) => datum.sample >= MIN_CONFIDENT_SAMPLE);
  const subject = confident.reduce<EmphasisDatum | null>(
    (best, datum) => (best === null || datum.rate > best.rate ? datum : best),
    null,
  );
  const ceiling = data.reduce((largest, datum) => Math.max(largest, datum.rate), 0);

  return (
    <div className={cn('flex flex-col gap-2', className)}>
      <ol className="flex flex-col gap-1.5">
        {data.map((datum) => {
          const isSubject = subject !== null && subject.key === datum.key;
          const width = ceiling === 0 ? 0 : (datum.rate / ceiling) * 100;
          const thin = datum.sample < MIN_CONFIDENT_SAMPLE;

          return (
            <li key={datum.key} className="flex items-center gap-3">
              <span className="w-[92px] shrink-0 truncate text-sm text-secondary">
                {datum.label}
              </span>
              <span className="min-w-0 flex-1">
                <span
                  className="block rounded-r-xs"
                  style={{
                    height: 20,
                    width: `${String(Math.max(width, datum.rate > 0 ? 1.5 : 0))}%`,
                    backgroundColor: emphasisColor(isSubject),
                  }}
                />
              </span>
              <span className="w-[52px] shrink-0 text-right font-mono text-mini tabular-nums text-primary">
                {formatPercent(datum.rate)}
              </span>
              <span
                className={cn(
                  'w-[64px] shrink-0 text-right font-mono text-micro tracking-normal',
                  thin ? 'text-st-review' : 'text-muted',
                )}
                title={
                  thin
                    ? `Only ${String(datum.sample)} applications in this band — too few to draw a conclusion from.`
                    : `${String(datum.sample)} applications in this band.`
                }
              >
                n = {datum.sample}
              </span>
            </li>
          );
        })}
      </ol>
      {footnote !== undefined && <p className="text-mini text-muted">{footnote}</p>}
    </div>
  );
}
