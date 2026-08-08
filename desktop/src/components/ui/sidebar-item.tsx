/**
 * SidebarItem — `custom` (`docs/UI.md` §5.3 and §7.23).
 *
 * | State | Background | Text | Icon | Rail |
 * |---|---|---|---|---|
 * | default | transparent | `--fg-secondary` | `--fg-muted` | none |
 * | hover | `--state-hover` | `--fg-primary` | `--fg-secondary` | none |
 * | active | `--accent-subtle` | `--fg-primary` (510) | `--accent-text` | 2×14px `--accent` |
 *
 * `transition: background-color` and nothing else. **No transform, no icon scale, and the
 * rail appears on the same frame** — navigation is the most frequent action in the app and
 * §6.4 gives it `--dur-0`. Chrome recedes; it does not perform.
 *
 * Icons are 16px with **no coloured backgrounds**, and the label is `--fg-secondary` rather
 * than `--fg-primary`: the sidebar should not compete for attention it has not earned.
 *
 * The count badge turns `--st-review` with a tinted pill when the review queue is non-empty.
 * That is the one place in the chrome where colour appears unprompted, and it is the number
 * the morning read exists for.
 */

import type { LucideIcon } from 'lucide-react';
import { forwardRef } from 'react';

import type { Combo } from '@/lib/shortcuts';
import { cn } from '@/lib/utils';

import { KbdCombo } from './kbd';
import { Tooltip } from './tooltip';

/** Props for {@link SidebarItem}. */
export interface SidebarItemProps extends React.ComponentPropsWithoutRef<'button'> {
  icon: LucideIcon;
  label: string;
  active?: boolean;
  /** Right-aligned count. Rendered in mono; `0` is hidden rather than shown as a zero. */
  count?: number;
  /** Makes a non-zero count `--st-review` with a tinted pill. The review queue only. */
  countIsUrgent?: boolean;
  /** The `g`-chord that reaches this destination. Shown in the collapsed tooltip. */
  shortcut?: Combo;
  /** Icon-only, 52px wide, with the label in a 400ms-delayed tooltip. */
  collapsed?: boolean;
}

/** One navigation destination. */
export const SidebarItem = forwardRef<HTMLButtonElement, SidebarItemProps>(function SidebarItem(
  {
    icon: Icon,
    label,
    active = false,
    count,
    countIsUrgent = false,
    shortcut,
    collapsed = false,
    className,
    ...props
  },
  ref,
) {
  const showCount = count !== undefined && count > 0;
  const urgent = showCount && countIsUrgent;

  const button = (
    <button
      ref={ref}
      type="button"
      aria-current={active ? 'page' : undefined}
      className={cn(
        'relative flex h-7 w-full items-center rounded-md text-sm',
        'transition-colors duration-[140ms] ease-out-quad',
        collapsed ? 'justify-center px-0' : 'gap-2 px-2',
        active
          ? 'bg-accent-subtle font-medium text-primary hover:bg-accent/[0.16]'
          : 'text-secondary hover:bg-state-hover hover:text-primary',
        className,
      )}
      {...props}
    >
      {/* 2×14px accent rail, vertically centred. Absolutely positioned so it can never
          affect flow, and it has no colour transition — it appears on the same frame. */}
      {active && (
        <span
          aria-hidden="true"
          className="absolute left-0 top-1/2 h-3.5 w-0.5 -translate-y-1/2 rounded-full bg-accent"
        />
      )}

      <Icon
        size={16}
        strokeWidth={1.5}
        aria-hidden="true"
        className={cn('shrink-0', active ? 'text-accent-text' : 'text-muted')}
      />

      {!collapsed && (
        <>
          <span className="min-w-0 flex-1 truncate text-left">{label}</span>
          {showCount && (
            <span
              className={cn(
                'shrink-0 font-mono text-mini tabular-nums',
                urgent
                  ? 'rounded-full bg-st-review/[0.14] px-1.5 py-px text-st-review'
                  : 'text-muted',
              )}
            >
              {count}
            </span>
          )}
        </>
      )}
    </button>
  );

  if (!collapsed) return button;

  return (
    <Tooltip
      content={label}
      side="right"
      shortcut={shortcut === undefined ? undefined : <KbdCombo combo={shortcut} />}
    >
      {button}
    </Tooltip>
  );
});

/** A `.label-caps` group heading. 24px tall, 12px of top margin (§5.3). */
export function SidebarGroupLabel({
  className,
  ...props
}: React.ComponentPropsWithoutRef<'div'>) {
  return <div className={cn('label-caps mt-3 flex h-6 items-center px-2', className)} {...props} />;
}
