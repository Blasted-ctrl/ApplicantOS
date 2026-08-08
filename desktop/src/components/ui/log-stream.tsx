/**
 * LogStream — `custom`. The most performance-sensitive component in the app
 * (`docs/UI.md` §7.21).
 *
 * ```
 * ┌──────────────────────────────────────────────────────────────────────────┐
 * │ 09:41:02.113  INFO   pipeline   posting.scored posting_id=8f3a… total=82 │ 20px line
 * │ 09:41:04.902  ERROR  apply      submit_not_found provider=workday        │
 * │                          ⟨ 14 new lines ↓ ⟩                              │ pill, detached
 * └──────────────────────────────────────────────────────────────────────────┘
 * ```
 *
 * Five binding behaviours, and every one of them exists because the naive version fails at
 * a few hundred lines a second:
 *
 * 1. **Never a query.** The tail is the module-level ring buffer in `lib/log-buffer.ts`,
 *    capped at 10 000 lines and never persisted.
 * 2. **rAF-coalesced flush**, dropping to 250ms while the reader is scrolled away — the
 *    buffer owns that, and {@link LogStream} tells it which state it is in.
 * 3. **Stick to the bottom only within 50px of it.** Otherwise freeze and show the
 *    `N new lines ↓` pill. The threshold is what distinguishes a deliberate scroll-up from
 *    layout jitter; without it, one stray wheel event unpins the view.
 * 4. **It is a tool, not a feed.** Level filter, source filter, follow toggle, copy, clear —
 *    supplied by the caller into the 36px tool row.
 * 5. **`aria-live="polite" aria-atomic="false"`** on the viewport, so a screen reader
 *    announces increments rather than re-reading ten thousand lines.
 *
 * **No motion on lines, ever.** No `AnimatePresence`, no enter transition, no stagger.
 */

import { useVirtualizer } from '@tanstack/react-virtual';
import { ArrowDown } from 'lucide-react';
import { useCallback, useEffect, useLayoutEffect, useRef, useState, type ReactNode } from 'react';

import { useLogStream, useLogStreamControls } from '@/hooks/use-log-stream';
import type { LogLine } from '@/lib/log-buffer';
import { cn } from '@/lib/utils';

/** Line height (§4.2). Fixed, because the virtualiser must never measure. */
const LINE_HEIGHT = 20;

/** Rows rendered beyond the window. Higher than a table's, because lines are half as tall. */
const OVERSCAN = 20;

/** Distance from the bottom within which the view stays pinned. */
const STICK_THRESHOLD_PX = 50;

/** Level → colour, per §7.21. `CRITICAL` additionally washes its whole row. */
function levelClass(level: string): string {
  switch (level.toUpperCase()) {
    case 'DEBUG':
      return 'text-muted';
    case 'WARNING':
      return 'text-st-review';
    case 'ERROR':
    case 'CRITICAL':
      return 'text-st-danger';
    default:
      return 'text-secondary';
  }
}

/** Props for {@link LogStream}. */
export interface LogStreamProps extends React.ComponentPropsWithoutRef<'div'> {
  /** The 36px tool row: search, level filter, source filter, copy, follow toggle. */
  toolbar?: ReactNode;
  /** Only lines whose level is in this set render. Omit for all levels. */
  levels?: ReadonlySet<string>;
  /** Case-insensitive substring, or a regular expression when `useRegex` is set. */
  search?: string;
  useRegex?: boolean;
  /** Show the millisecond-resolution timestamp column. */
  showTimestamps?: boolean;
}

/** The live tail. */
export function LogStream({
  toolbar,
  levels,
  search = '',
  useRegex = false,
  showTimestamps = true,
  className,
  ...props
}: LogStreamProps) {
  const lines = useLogStream();
  const controls = useLogStreamControls();
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const [pinned, setPinned] = useState(true);
  const [unseen, setUnseen] = useState(0);

  const matcher = useCallback(
    (line: LogLine): boolean => {
      if (levels !== undefined && !levels.has(line.level.toUpperCase())) return false;
      if (search === '') return true;
      const haystack = `${line.event} ${line.logger ?? ''}`;
      if (!useRegex) return haystack.toLowerCase().includes(search.toLowerCase());
      try {
        return new RegExp(search, 'i').test(haystack);
      } catch {
        // An incomplete regex while the user is still typing is not an error state; it
        // simply matches nothing until it parses.
        return false;
      }
    },
    [levels, search, useRegex],
  );

  const visible = lines.filter(matcher);

  const virtualizer = useVirtualizer({
    count: visible.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => LINE_HEIGHT,
    overscan: OVERSCAN,
    getItemKey: (index) => visible[index]?.seq ?? index,
  });

  // Layout effect, not effect: scrolling after paint would show one frame of the old
  // position, which at frame rate reads as a stutter.
  useLayoutEffect(() => {
    if (!pinned || visible.length === 0) return;
    virtualizer.scrollToIndex(visible.length - 1, { align: 'end' });
    setUnseen(0);
  }, [pinned, visible.length, virtualizer]);

  useEffect(() => {
    if (pinned) return;
    setUnseen((count) => count + 1);
  }, [pinned, visible.length]);

  useEffect(() => {
    controls.setFollowing(pinned);
  }, [controls, pinned]);

  const onScroll = useCallback(() => {
    const node = scrollRef.current;
    if (node === null) return;
    const distance = node.scrollHeight - node.scrollTop - node.clientHeight;
    setPinned(distance <= STICK_THRESHOLD_PX);
  }, []);

  const jumpToBottom = useCallback(() => {
    setPinned(true);
    setUnseen(0);
    if (visible.length > 0) virtualizer.scrollToIndex(visible.length - 1, { align: 'end' });
  }, [virtualizer, visible.length]);

  const items = virtualizer.getVirtualItems();
  const offsetY = items[0]?.start ?? 0;

  return (
    <div
      className={cn(
        'relative flex min-h-0 flex-col overflow-hidden rounded-lg border border-default bg-inset',
        className,
      )}
      {...props}
    >
      {toolbar !== undefined && (
        <div className="flex h-9 shrink-0 items-center gap-2 border-b border-default px-2">
          {toolbar}
        </div>
      )}

      <div
        ref={scrollRef}
        onScroll={onScroll}
        role="log"
        aria-live="polite"
        aria-atomic="false"
        aria-label="Live log output"
        // `contain: strict` keeps a ten-thousand-line list from participating in the page's
        // layout and paint at all.
        className="scroll-region min-h-0 flex-1 [contain:strict]"
      >
        <div style={{ height: virtualizer.getTotalSize(), position: 'relative' }}>
          <div style={{ transform: `translateY(${String(offsetY)}px)` }}>
            {items.map((item) => {
              const line = visible[item.index];
              if (line === undefined) return null;
              const critical = line.level.toUpperCase() === 'CRITICAL';
              return (
                <div
                  key={line.seq}
                  className={cn(
                    'flex items-center gap-2 whitespace-pre px-2 font-mono text-mini tracking-normal tabular-nums',
                    'hover:bg-state-hover',
                    critical && 'bg-st-danger/[0.10]',
                  )}
                  style={{ height: LINE_HEIGHT }}
                >
                  {showTimestamps && (
                    <span className="w-24 shrink-0 text-muted">{line.at.slice(11, 23)}</span>
                  )}
                  <span className={cn('w-14 shrink-0', levelClass(line.level))}>
                    {line.level.toUpperCase()}
                  </span>
                  <span className="w-22 shrink-0 truncate text-secondary">
                    {line.logger ?? ''}
                  </span>
                  <span className="min-w-0 flex-1 truncate text-primary">{line.event}</span>
                </div>
              );
            })}
          </div>
        </div>
      </div>

      {/* The detached pill. Bottom-centred, above the lines, never covering the last one. */}
      {!pinned && unseen > 0 && (
        <button
          type="button"
          onClick={jumpToBottom}
          className={cn(
            'absolute bottom-3 left-1/2 -translate-x-1/2',
            'inline-flex h-6 items-center gap-1.5 rounded-full border border-strong bg-overlay px-2.5',
            'font-mono text-micro tracking-normal text-secondary shadow-float',
            'hover:text-primary',
          )}
        >
          {unseen} new lines
          <ArrowDown className="size-3" aria-hidden="true" />
        </button>
      )}
    </div>
  );
}
