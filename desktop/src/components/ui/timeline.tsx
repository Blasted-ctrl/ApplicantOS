/**
 * Timeline — `custom` (`docs/UI.md` §7.20).
 *
 * ```
 *    ●───┐  09:41:02   Application created                    mono 11 --fg-muted
 *    │   └─ posting scored 82 · greenhouse                    13 --fg-secondary
 *    ●───┐  09:41:18   Resume tailored                ⟨ v3 ⟩
 *    ◉───┐  09:41:44   Submitting…                            in-flight: pulsing
 *    ○      —          Awaiting confirmation
 * ```
 *
 * Two behaviours are load-bearing.
 *
 * **A new event fades in over 180ms — no slide, no stagger.** Events arrive live over the
 * WebSocket, and a list that reshuffles or cascades every time one lands is unreadable while
 * a run is going. Opacity only, `V.fade` + `T.pop`.
 *
 * **The timeline is bound to the selected entity, never to scroll position.** It is a record
 * of what happened to *this* application; a scroll-driven reveal would make the record look
 * like it was still being written.
 *
 * The time column is a fixed 64px in mono, which is what keeps titles left-aligned with each
 * other whether the timestamp is `09:41:02` or `—`.
 */

import { motion } from 'framer-motion';
import type { ReactNode } from 'react';

import { T, V } from '@/lib/motion';
import { cn, EM_DASH, formatIso, formatTime } from '@/lib/utils';

import { StatusDot } from './status-dot';

/** One node. */
export interface TimelineItem {
  id: string;
  /** ISO timestamp, or `null` for a step that has not happened yet. */
  at: string | null;
  title: string;
  /** One line of detail. May contain mono chips. */
  detail?: ReactNode;
  /**
   * The application status this node represents, which decides the dot's shape and whether
   * it pulses. Omit for a step that is merely pending.
   */
  status?: React.ComponentProps<typeof StatusDot>['status'];
  /** Right-aligned metadata: a version chip, a duration. */
  meta?: ReactNode;
  /** A node that has not happened: hollow dot, disabled text, lighter rail. */
  pending?: boolean;
}

/** Props for {@link Timeline}. */
export interface TimelineProps extends React.ComponentPropsWithoutRef<'ol'> {
  items: readonly TimelineItem[];
  /**
   * Animate entries in.
   *
   * True for a live application detail; false when the timeline is inside a virtualised or
   * collapsible surface, where mounting animations fight the container.
   */
  animate?: boolean;
}

/** A vertical event log for one entity. */
export function Timeline({ items, animate = true, className, ...props }: TimelineProps) {
  return (
    <ol className={cn('relative flex flex-col', className)} {...props}>
      {items.map((item, index) => {
        const last = index === items.length - 1;
        const content = (
          <>
            {/* Rail and node. The rail stops at the last item so the list does not appear
                to continue past its end. */}
            <span className="relative flex w-2 shrink-0 justify-center pt-1">
              {item.status === undefined ? (
                <span
                  aria-hidden="true"
                  className={cn(
                    'size-2 rounded-full border-[1.5px]',
                    item.pending === true ? 'border-disabled' : 'border-strong',
                  )}
                />
              ) : (
                <StatusDot status={item.status} />
              )}
              {!last && (
                <span
                  aria-hidden="true"
                  className={cn(
                    'absolute left-1/2 top-4 -ml-px w-px',
                    item.pending === true ? 'bg-subtle' : 'bg-default',
                  )}
                  style={{ height: 'calc(100% - 8px)' }}
                />
              )}
            </span>

            <time
              className="w-16 shrink-0 font-mono text-micro tracking-normal tabular-nums text-muted"
              dateTime={item.at ?? undefined}
              title={item.at === null ? undefined : formatIso(item.at)}
            >
              {item.at === null ? EM_DASH : formatTime(item.at)}
            </time>

            <span className="min-w-0 flex-1">
              <span className="flex items-baseline justify-between gap-3">
                <span
                  className={cn(
                    'text-sm font-medium',
                    item.pending === true ? 'text-disabled' : 'text-primary',
                  )}
                >
                  {item.title}
                </span>
                {item.meta !== undefined && <span className="shrink-0">{item.meta}</span>}
              </span>
              {item.detail !== undefined && (
                <span className="mt-1 block text-mini text-muted">{item.detail}</span>
              )}
            </span>
          </>
        );

        return animate ? (
          <motion.li
            key={item.id}
            className="flex gap-3 pb-4 last:pb-0"
            initial={V.fade.initial}
            animate={V.fade.animate}
            transition={T.pop}
          >
            {content}
          </motion.li>
        ) : (
          <li key={item.id} className="flex gap-3 pb-4 last:pb-0">
            {content}
          </li>
        );
      })}
    </ol>
  );
}
