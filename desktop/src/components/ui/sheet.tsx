/**
 * Sheet — `shadcn` (Radix `Dialog`), re-skinned (`docs/UI.md` §7.13).
 *
 * A right-side drawer, 480px (`lg` 640, `xl` 60vw), full height minus the titlebar. Enters
 * with `V.sheetR` + `T.sheet` — the iOS `[0.32, 0.72, 0, 1]` curve, which is the one curve in
 * the system for anything that slides or resizes.
 *
 * **Drag-to-dismiss** from the left edge, with two behaviours that are what separate a drawer
 * that feels physical from one that feels like a div: damping past the boundary (dragging it
 * further right than it can go resists rather than stops), and **velocity dismissal above
 * 0.11 px/ms regardless of distance** — a fast flick closes it even from 10px, because the
 * user's intent was unambiguous.
 *
 * **Under reduced motion the sheet fades in place** (§6.7). Removing the transform rather
 * than shortening it is the policy: a 320px slide at 0.01ms is still a 320px slide.
 *
 * **Usage rule (§7.13):** a sheet holds a working surface that keeps the list visible behind
 * it. If the user needs the list *and* the detail simultaneously, use the master–detail
 * layout instead.
 */

import * as DialogPrimitive from '@radix-ui/react-dialog';
import { AnimatePresence, motion, useReducedMotion, type PanInfo } from 'framer-motion';
import { X } from 'lucide-react';
import { forwardRef, type ReactNode } from 'react';

import { T, V } from '@/lib/motion';
import { cn } from '@/lib/utils';

import { Button } from './button';

/** Sheet root. */
export const Sheet = DialogPrimitive.Root;

/** The element that opens the sheet. */
export const SheetTrigger = DialogPrimitive.Trigger;

/** Closes the sheet. */
export const SheetClose = DialogPrimitive.Close;

/** Widths (§7.13). */
const WIDTHS = { md: '480px', lg: '640px', xl: '60vw' } as const;

/**
 * Velocity above which a drag dismisses regardless of distance.
 *
 * `docs/UI.md` states 0.11 px/ms; Motion reports velocity in px/s, hence the ×1000.
 */
const DISMISS_VELOCITY_PX_PER_S = 110;

/** Distance past which a slow drag dismisses, as a fraction of the sheet's width. */
const DISMISS_DISTANCE_RATIO = 0.4;

/** Props for {@link SheetContent}. */
export interface SheetContentProps
  extends Omit<React.ComponentPropsWithoutRef<typeof DialogPrimitive.Content>, 'title'> {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  size?: keyof typeof WIDTHS;
  title: ReactNode;
  description?: ReactNode;
  /** Sticky footer, for the sheet's primary action. */
  footer?: ReactNode;
}

/** The drawer surface. */
export const SheetContent = forwardRef<
  React.ComponentRef<typeof DialogPrimitive.Content>,
  SheetContentProps
>(function SheetContent(
  { open, onOpenChange, size = 'md', title, description, footer, className, children, ...props },
  ref,
) {
  const reduce = useReducedMotion();
  const slide = reduce !== true;

  const onDragEnd = (_event: unknown, info: PanInfo): void => {
    const flicked = info.velocity.x > DISMISS_VELOCITY_PX_PER_S;
    const dragged = info.offset.x > window.innerWidth * DISMISS_DISTANCE_RATIO;
    if (flicked || dragged) onOpenChange(false);
  };

  return (
    <AnimatePresence>
      {open && (
        <DialogPrimitive.Portal forceMount>
          <DialogPrimitive.Overlay asChild forceMount>
            <motion.div
              className="scrim fixed inset-0 z-50"
              initial={V.fade.initial}
              animate={V.fade.animate}
              exit={V.fade.exit}
              transition={T.sheet}
            />
          </DialogPrimitive.Overlay>

          <DialogPrimitive.Content ref={ref} asChild forceMount {...props}>
            <motion.div
              className={cn(
                'accent-hairline fixed inset-y-0 right-0 z-50 flex flex-col',
                'rounded-l-xl border-l border-strong bg-overlay shadow-dialog',
                'max-w-[calc(100vw-64px)]',
                className,
              )}
              style={{ width: WIDTHS[size], top: 'var(--h-titlebar)' }}
              initial={slide ? V.sheetR.initial : V.fade.initial}
              animate={slide ? V.sheetR.animate : V.fade.animate}
              exit={slide ? V.sheetR.exit : V.fade.exit}
              transition={T.sheet}
              drag={slide ? 'x' : false}
              dragDirectionLock
              dragConstraints={{ left: 0, right: 0 }}
              dragElastic={{ left: 0, right: 0.25 }}
              dragMomentum={false}
              onDragEnd={onDragEnd}
            >
              <header className="flex h-14 shrink-0 items-center justify-between gap-3 px-5">
                <div className="min-w-0">
                  <DialogPrimitive.Title className="truncate font-display text-md font-semibold text-primary">
                    {title}
                  </DialogPrimitive.Title>
                  {description !== undefined && (
                    <DialogPrimitive.Description className="truncate text-sm text-muted">
                      {description}
                    </DialogPrimitive.Description>
                  )}
                </div>
                <DialogPrimitive.Close asChild>
                  <Button variant="ghost" size="sm" icon aria-label="Close">
                    <X aria-hidden="true" />
                  </Button>
                </DialogPrimitive.Close>
              </header>

              <div className="scroll-region min-h-0 flex-1 px-5 pb-5">{children}</div>

              {footer !== undefined && (
                <footer className="flex shrink-0 items-center justify-end gap-2 border-t border-state-divider p-4">
                  {footer}
                </footer>
              )}
            </motion.div>
          </DialogPrimitive.Content>
        </DialogPrimitive.Portal>
      )}
    </AnimatePresence>
  );
});
