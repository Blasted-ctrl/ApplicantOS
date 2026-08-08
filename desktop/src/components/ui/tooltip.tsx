/**
 * Tooltip — `shadcn` (Radix `Tooltip`), re-skinned (`docs/UI.md` §7.14).
 *
 * 400ms on first hover, and **0ms with no animation** for any subsequent tooltip within 300ms
 * of the last one closing. That second rule is what makes a toolbar feel responsive rather
 * than syrupy: once the user has committed to reading tooltips, making them wait 400ms again
 * for the next button is a delay they did not ask for. Radix's `TooltipProvider` implements
 * exactly this with `delayDuration` and `skipDelayDuration`; the `data-instant` attribute
 * carries it through to the animation, which `styles/globals.css` zeroes.
 *
 * **Usage rule (§7.14):** tooltips explain, they do not contain. No links, no buttons, no
 * wrapping paragraphs. Every icon-only button **must** have one, and its text must equal the
 * button's `aria-label` — two different strings for the same control is how a screen-reader
 * user and a sighted user end up with different products.
 */

import * as TooltipPrimitive from '@radix-ui/react-tooltip';
import { forwardRef, type ReactNode } from 'react';

import { cn } from '@/lib/utils';

/** Delay before the first tooltip in a group opens. */
const DELAY_MS = 400;

/** Window in which a following tooltip opens instantly. */
const SKIP_DELAY_MS = 300;

/** Props for {@link TooltipProvider}. */
export type TooltipProviderProps = React.ComponentPropsWithoutRef<
  typeof TooltipPrimitive.Provider
>;

/** Wrap the app once. The delay behaviour is a property of the *group*, not of one tooltip. */
export function TooltipProvider({
  delayDuration = DELAY_MS,
  skipDelayDuration = SKIP_DELAY_MS,
  ...props
}: TooltipProviderProps) {
  return (
    <TooltipPrimitive.Provider
      delayDuration={delayDuration}
      skipDelayDuration={skipDelayDuration}
      {...props}
    />
  );
}

/** Tooltip root. */
export const TooltipRoot = TooltipPrimitive.Root;

/** The element the tooltip describes. */
export const TooltipTrigger = TooltipPrimitive.Trigger;

/** The floating panel. Level 4: `--bg-overlay`, `--border-strong`, `--shadow-float`. */
export const TooltipContent = forwardRef<
  React.ComponentRef<typeof TooltipPrimitive.Content>,
  React.ComponentPropsWithoutRef<typeof TooltipPrimitive.Content>
>(function TooltipContent({ className, sideOffset = 6, ...props }, ref) {
  return (
    <TooltipPrimitive.Portal>
      <TooltipPrimitive.Content
        ref={ref}
        sideOffset={sideOffset}
        className={cn(
          'overlay-pop z-50 max-w-[280px] rounded-md border border-strong bg-overlay',
          'px-2 py-1 text-mini text-primary shadow-float',
          'origin-(--radix-tooltip-content-transform-origin)',
          className,
        )}
        {...props}
      />
    </TooltipPrimitive.Portal>
  );
});

/** Props for {@link Tooltip}. */
export interface TooltipProps {
  /** The explanation. One short phrase — a tooltip that wraps is a popover in disguise. */
  content: ReactNode;
  children: ReactNode;
  side?: 'top' | 'right' | 'bottom' | 'left';
  align?: 'start' | 'center' | 'end';
  /** Rendered right-aligned inside the tooltip, normally a `Kbd`. */
  shortcut?: ReactNode;
}

/**
 * The convenience form: trigger plus content in one element.
 *
 * `asChild` on the trigger, so the tooltip does not insert a wrapper that would break a flex
 * row's spacing or a table cell's truncation.
 */
export function Tooltip({ content, children, side = 'top', align = 'center', shortcut }: TooltipProps) {
  return (
    <TooltipRoot>
      <TooltipTrigger asChild>{children}</TooltipTrigger>
      <TooltipContent side={side} align={align}>
        <span className="flex items-center gap-2">
          <span>{content}</span>
          {shortcut}
        </span>
      </TooltipContent>
    </TooltipRoot>
  );
}
