/**
 * Select — `shadcn` (Radix `Select`), re-skinned (`docs/UI.md` §7.3).
 *
 * The trigger is **identical to Input** — same heights, background, border and radius — plus
 * a 14px chevron in `--fg-muted`. That is deliberate: a select is a field, and a field that
 * looked like a button would invite a click expecting an action.
 *
 * The panel is level 4 of the elevation ladder: `--bg-overlay`, `--border-strong`,
 * `--shadow-float`. Motion is the CSS form of `V.popIn` + `T.pop` / `T.popOut` (the
 * `.overlay-pop` class in `styles/globals.css`) with
 * `transform-origin: var(--radix-select-content-transform-origin)`, so the panel grows out of
 * its trigger rather than out of its own centre.
 *
 * **Usage rule (§7.3):** ≤ 8 options → Select. More than 8 → a Combobox. More than 40 → the
 * command palette scoped to that field. Never a native `<select>` — it cannot be styled to
 * the token system on Windows and would be the one control in the app that looks foreign.
 */

import * as SelectPrimitive from '@radix-ui/react-select';
import { Check, ChevronDown, ChevronUp } from 'lucide-react';
import { forwardRef } from 'react';

import { cn } from '@/lib/utils';

import { fieldVariants } from './variants';

/** Root. Controlled or uncontrolled, exactly as Radix defines it. */
export const Select = SelectPrimitive.Root;

/** Groups items under a `.label-caps` heading. */
export const SelectGroup = SelectPrimitive.Group;

/** The selected value, or the placeholder. */
export const SelectValue = SelectPrimitive.Value;

/** Props for {@link SelectTrigger}. */
export interface SelectTriggerProps
  extends React.ComponentPropsWithoutRef<typeof SelectPrimitive.Trigger> {
  size?: 'sm' | 'md' | 'lg';
}

/** The field-shaped trigger. */
export const SelectTrigger = forwardRef<
  React.ComponentRef<typeof SelectPrimitive.Trigger>,
  SelectTriggerProps
>(function SelectTrigger({ className, size = 'md', children, ...props }, ref) {
  return (
    <SelectPrimitive.Trigger
      ref={ref}
      className={cn(
        fieldVariants({ size }),
        'justify-between gap-1.5 text-left',
        'data-[placeholder]:text-muted',
        className,
      )}
      {...props}
    >
      <span className="min-w-0 flex-1 truncate">{children}</span>
      <SelectPrimitive.Icon asChild>
        <ChevronDown className="size-3.5 shrink-0 text-muted" aria-hidden="true" />
      </SelectPrimitive.Icon>
    </SelectPrimitive.Trigger>
  );
});

/** The dropdown panel. Portalled, so it is never clipped by a table's `overflow: auto`. */
export const SelectContent = forwardRef<
  React.ComponentRef<typeof SelectPrimitive.Content>,
  React.ComponentPropsWithoutRef<typeof SelectPrimitive.Content>
>(function SelectContent({ className, children, position = 'popper', ...props }, ref) {
  return (
    <SelectPrimitive.Portal>
      <SelectPrimitive.Content
        ref={ref}
        position={position}
        sideOffset={4}
        className={cn(
          'overlay-pop relative z-50 max-h-[320px] min-w-[8rem] overflow-hidden',
          'rounded-lg border border-strong bg-overlay p-1 shadow-float',
          'origin-(--radix-select-content-transform-origin)',
          position === 'popper' && 'w-full min-w-(--radix-select-trigger-width)',
          className,
        )}
        {...props}
      >
        <SelectPrimitive.ScrollUpButton className="flex h-5 items-center justify-center text-muted">
          <ChevronUp className="size-3.5" aria-hidden="true" />
        </SelectPrimitive.ScrollUpButton>
        <SelectPrimitive.Viewport className="p-0">{children}</SelectPrimitive.Viewport>
        <SelectPrimitive.ScrollDownButton className="flex h-5 items-center justify-center text-muted">
          <ChevronDown className="size-3.5" aria-hidden="true" />
        </SelectPrimitive.ScrollDownButton>
      </SelectPrimitive.Content>
    </SelectPrimitive.Portal>
  );
});

/** A caps eyebrow above a group of items. */
export const SelectLabel = forwardRef<
  React.ComponentRef<typeof SelectPrimitive.Label>,
  React.ComponentPropsWithoutRef<typeof SelectPrimitive.Label>
>(function SelectLabel({ className, ...props }, ref) {
  return (
    <SelectPrimitive.Label
      ref={ref}
      className={cn('label-caps flex h-6 items-center px-2', className)}
      {...props}
    />
  );
});

/**
 * One option.
 *
 * 28px, `--radius-md`. Highlighted uses `--state-hover` with **no transition** — the
 * highlight has to track arrow keys at key-repeat rate, and a 140ms fade turns a held arrow
 * key into a smear.
 */
export const SelectItem = forwardRef<
  React.ComponentRef<typeof SelectPrimitive.Item>,
  React.ComponentPropsWithoutRef<typeof SelectPrimitive.Item>
>(function SelectItem({ className, children, ...props }, ref) {
  return (
    <SelectPrimitive.Item
      ref={ref}
      className={cn(
        'relative flex h-7 cursor-default select-none items-center gap-2 rounded-md px-2 pr-7',
        'text-base text-primary outline-none',
        'data-[highlighted]:bg-state-hover',
        'data-[state=checked]:font-medium',
        'data-[disabled]:pointer-events-none data-[disabled]:text-disabled',
        className,
      )}
      {...props}
    >
      <SelectPrimitive.ItemText>{children}</SelectPrimitive.ItemText>
      <SelectPrimitive.ItemIndicator className="absolute right-2 inline-flex items-center">
        <Check className="size-3.5 text-accent-text" aria-hidden="true" />
      </SelectPrimitive.ItemIndicator>
    </SelectPrimitive.Item>
  );
});

/** A divider between item groups. One step below `--border-subtle`, per §4.5. */
export const SelectSeparator = forwardRef<
  React.ComponentRef<typeof SelectPrimitive.Separator>,
  React.ComponentPropsWithoutRef<typeof SelectPrimitive.Separator>
>(function SelectSeparator({ className, ...props }, ref) {
  return (
    <SelectPrimitive.Separator
      ref={ref}
      className={cn('-mx-1 my-1 h-px bg-state-divider', className)}
      {...props}
    />
  );
});
