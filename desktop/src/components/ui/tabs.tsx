/**
 * Tabs and SegmentedControl — `shadcn+` (Radix `Tabs`), re-skinned (`docs/UI.md` §7.11).
 *
 * Underline style for **detail-view lenses** (Timeline / Answers / Documents / Artifacts /
 * Activity), pill style for **choices** (density, chart range).
 *
 * ```
 *   Timeline   Answers   Documents   Artifacts   Activity
 *   ─────────                                              2px --accent, layoutId indicator
 * ─────────────────────────────────────────────────────    1px --border-subtle rail
 * ```
 *
 * **`layoutId` is permitted on exactly one element per view** (§6.4), and this indicator is
 * one of the two places it is allowed. It is a single 2px bar; a shared-layout animation
 * across the tab *content* would run a FLIP cycle with forced synchronous layout reads on
 * every participant, which is precisely what §6.4 bans.
 *
 * **Tab content does not animate.** It swaps. Switching a lens is a high-frequency action and
 * §6.4 gives it `--dur-0`.
 *
 * **Usage rule (§7.11):** tabs are lenses over *one object*. Switching a tab must never lose
 * the selected row or the scroll position, and must never change the route.
 */

import * as TabsPrimitive from '@radix-ui/react-tabs';
import { motion } from 'framer-motion';
import { createContext, forwardRef, useContext, useId } from 'react';

import { T } from '@/lib/motion';
import { cn } from '@/lib/utils';

/**
 * The `layoutId` namespace for one Tabs instance.
 *
 * Two Tabs on one screen sharing a `layoutId` would make their indicators animate *between*
 * the two components, which looks like a bug and is one.
 */
const IndicatorContext = createContext<string>('tab-indicator');

/** Tabs root. Supplies a unique indicator namespace to its triggers. */
export const Tabs = forwardRef<
  React.ComponentRef<typeof TabsPrimitive.Root>,
  React.ComponentPropsWithoutRef<typeof TabsPrimitive.Root>
>(function Tabs({ className, children, ...props }, ref) {
  const id = useId();
  return (
    <IndicatorContext.Provider value={`tab-indicator-${id}`}>
      <TabsPrimitive.Root ref={ref} className={cn('flex flex-col', className)} {...props}>
        {children}
      </TabsPrimitive.Root>
    </IndicatorContext.Provider>
  );
});

/** The tab strip, with the 1px `--border-subtle` rail beneath it. */
export const TabsList = forwardRef<
  React.ComponentRef<typeof TabsPrimitive.List>,
  React.ComponentPropsWithoutRef<typeof TabsPrimitive.List>
>(function TabsList({ className, ...props }, ref) {
  return (
    <TabsPrimitive.List
      ref={ref}
      className={cn('flex items-center gap-4 border-b border-subtle', className)}
      {...props}
    />
  );
});

/** One tab. 30px, colour-only hover, and the shared 2px indicator when active. */
export const TabsTrigger = forwardRef<
  React.ComponentRef<typeof TabsPrimitive.Trigger>,
  React.ComponentPropsWithoutRef<typeof TabsPrimitive.Trigger>
>(function TabsTrigger({ className, children, ...props }, ref) {
  const layoutId = useContext(IndicatorContext);
  return (
    <TabsPrimitive.Trigger
      ref={ref}
      className={cn(
        'group relative inline-flex h-[30px] items-center whitespace-nowrap text-base',
        'text-secondary transition-colors duration-[140ms] ease-out-quad',
        'hover:text-primary',
        'data-[state=active]:font-medium data-[state=active]:text-primary',
        'disabled:pointer-events-none disabled:text-disabled',
        className,
      )}
      {...props}
    >
      {children}
      <span className="pointer-events-none absolute inset-x-0 -bottom-px h-0.5">
        <span className="hidden group-data-[state=active]:block">
          <motion.span
            layoutId={layoutId}
            transition={T.springSnap}
            className="block h-0.5 w-full rounded-full bg-accent"
          />
        </span>
      </span>
    </TabsPrimitive.Trigger>
  );
});

/** A tab panel. Swaps with no animation — see the module docstring. */
export const TabsContent = forwardRef<
  React.ComponentRef<typeof TabsPrimitive.Content>,
  React.ComponentPropsWithoutRef<typeof TabsPrimitive.Content>
>(function TabsContent({ className, ...props }, ref) {
  return <TabsPrimitive.Content ref={ref} className={cn('mt-4 outline-none', className)} {...props} />;
});

/** One option in a {@link SegmentedControl}. */
export interface SegmentOption<T extends string> {
  value: T;
  label: string;
  /** Announced label when `label` is a glyph rather than a word. */
  ariaLabel?: string;
}

/** Props for {@link SegmentedControl}. */
export interface SegmentedControlProps<T extends string> {
  options: readonly SegmentOption<T>[];
  value: T;
  onValueChange: (value: T) => void;
  /** Announced name for the group, e.g. `Row density`. */
  label: string;
  className?: string;
}

/**
 * The pill form: a 26px `--bg-inset` track with the active segment raised.
 *
 * The second and last permitted `layoutId` per view. The moving pill is `--bg-elevated` plus
 * `--shadow-raised` — the same elevation step a selected row gets, for the same reason: this
 * is structural selection, not hover.
 */
export function SegmentedControl<T extends string>({
  options,
  value,
  onValueChange,
  label,
  className,
}: SegmentedControlProps<T>) {
  const id = useId();
  return (
    <div
      role="radiogroup"
      aria-label={label}
      className={cn('inline-flex h-[26px] items-center gap-0 rounded-md bg-inset p-0.5', className)}
    >
      {options.map((option) => {
        const active = option.value === value;
        return (
          <button
            key={option.value}
            type="button"
            role="radio"
            aria-checked={active}
            aria-label={option.ariaLabel}
            onClick={() => {
              onValueChange(option.value);
            }}
            className={cn(
              'relative inline-flex h-[22px] items-center rounded-[5px] px-2 text-mini',
              'transition-colors duration-[140ms] ease-out-quad',
              active ? 'text-primary' : 'text-secondary hover:text-primary',
            )}
          >
            {active && (
              <motion.span
                layoutId={`segment-${id}`}
                transition={T.springSnap}
                className="absolute inset-0 rounded-[5px] bg-elevated shadow-raised"
              />
            )}
            <span className="relative z-10">{option.label}</span>
          </button>
        );
      })}
    </div>
  );
}
