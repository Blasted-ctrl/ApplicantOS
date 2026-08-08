/**
 * The discovery → offer funnel (`docs/UI.md` §11.6).
 *
 * Horizontal, descending, one row per stage, 20px thick, a 4px rounded right end and a square
 * left end at the baseline, a 2px gap in the surface colour between rows, and the conversion
 * from the previous stage rendered *between* the rows rather than inside them.
 *
 * **One hue, not a palette.** A funnel is a part-to-whole with an ordering, so the encoding is
 * position and length; giving each stage its own hue would imply the stages are categories
 * that could be reordered. And it is never a pie: §11.1 bans them outright.
 *
 * Built from divs rather than a chart library because it is thirty lines of layout and pulling
 * a rendering engine into it would cost more than it saves.
 */

import type { FunnelStage } from '@/lib/api/types';
import { sequentialColor } from '@/lib/chart/series';
import { cn, formatNumber, formatPercent } from '@/lib/utils';

/** Bar thickness in pixels. */
const BAR_HEIGHT = 20;

/** Props for {@link FunnelBar}. */
export interface FunnelBarProps {
  stages: readonly FunnelStage[];
  className?: string;
}

/** The funnel. */
export function FunnelBar({ stages, className }: FunnelBarProps) {
  const widest = stages.reduce((largest, stage) => Math.max(largest, stage.count), 0);

  return (
    <ol className={cn('flex flex-col', className)}>
      {stages.map((stage, index) => {
        const fraction = widest === 0 ? 0 : stage.count / widest;
        // Darkest at the top: the first stage takes the top of the ramp.
        const ramp = stages.length <= 1 ? 1 : 1 - index / (stages.length - 1);

        return (
          <li key={stage.key} className="flex flex-col">
            {index > 0 && (
              <span className="py-0.5 pl-[104px] font-mono text-micro tracking-normal text-muted">
                {formatPercent(stage.conversion_rate)} of the previous stage
              </span>
            )}
            <div className="flex items-center gap-3" style={{ marginBottom: 2 }}>
              <span className="w-[92px] shrink-0 truncate text-sm text-secondary">
                {stage.label}
              </span>
              <span className="min-w-0 flex-1">
                <span
                  className="block rounded-r-xs"
                  style={{
                    height: BAR_HEIGHT,
                    width: `${String(Math.max(fraction * 100, stage.count > 0 ? 1.5 : 0))}%`,
                    backgroundColor: sequentialColor(ramp),
                  }}
                />
              </span>
              <span className="w-[64px] shrink-0 text-right font-mono text-mini tabular-nums text-primary">
                {formatNumber(stage.count)}
              </span>
            </div>
          </li>
        );
      })}
    </ol>
  );
}
