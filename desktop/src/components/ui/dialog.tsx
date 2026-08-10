/**
 * Dialog — `shadcn` (Radix `Dialog`), re-skinned (`docs/UI.md` §7.12).
 *
 * Level 5: `--bg-overlay`, `--border-strong`, `--radius-xl`, `--shadow-dialog`, over a
 * `rgb(6 7 10 / 0.55)` scrim with `blur(3px)` — one of only two places backdrop blur is
 * permitted (§4.4).
 *
 * The 1px accent hairline across the top edge is **the one sanctioned gradient in the
 * product** (§2.7 rule 6). It is `.accent-hairline` in `styles/globals.css` and it exists
 * nowhere else except the sheet and the command palette.
 *
 * Motion uses the named objects — `V.dialog` + `T.dialog` in, `T.dialogOut` out — rather than
 * the CSS `data-state` path the popovers use. A dialog is rare and high-ceremony, so the cost
 * of an `AnimatePresence` tree is worth the exactness; and `transform-origin: center` is the
 * documented exception to origin-awareness, because a modal did not come from anywhere.
 *
 * **Usage rule (§7.12):** dialogs are for *decisions* — confirm a destructive action, pick a
 * résumé variant, resolve one review field. Anything that is a *workspace* is a Sheet.
 */

import * as DialogPrimitive from '@radix-ui/react-dialog';
import { AnimatePresence, motion, useReducedMotion } from 'framer-motion';
import { X } from 'lucide-react';
import { forwardRef, type ReactNode } from 'react';

import { motionSafe, T, V } from '@/lib/motion';
import { cn } from '@/lib/utils';

import { Button } from './button';

/** Dialog root. Controlled, because `AnimatePresence` needs to know when it is closing. */
export const Dialog = DialogPrimitive.Root;

/** The element that opens the dialog. Focus returns here on close. */
export const DialogTrigger = DialogPrimitive.Trigger;

/** Closes the dialog. Used by `Cancel`. */
export const DialogClose = DialogPrimitive.Close;

/** Widths (§7.12). Capped so a dialog never touches the window edges. */
const WIDTHS = { sm: 400, md: 520, lg: 680 } as const;

/** Props for {@link DialogContent}. */
export interface DialogContentProps
  extends Omit<React.ComponentPropsWithoutRef<typeof DialogPrimitive.Content>, 'title'> {
  /** Whether the dialog is open. Required: the exit animation needs the transition, not the
   *  unmount. */
  open: boolean;
  size?: keyof typeof WIDTHS;
  /** Dialog title. Required — Radix warns without one, and a titleless decision is not one. */
  title: ReactNode;
  /** One line, sentence case. Announced as the dialog's description. */
  description?: ReactNode;
  /** Right-aligned footer: `Cancel` (secondary) then the primary action. */
  footer?: ReactNode;
  /** Hides the `✕`. Only for a dialog whose sole exit is an explicit decision. */
  hideClose?: boolean;
}

/**
 * The modal surface.
 *
 * `forceMount` on the portal, with `AnimatePresence` owning the unmount: without it Radix
 * removes the node the instant `open` flips and there is nothing left to animate out.
 */
export const DialogContent = forwardRef<
  React.ComponentRef<typeof DialogPrimitive.Content>,
  DialogContentProps
>(function DialogContent(
  { open, size = 'md', title, description, footer, hideClose = false, className, children, ...props },
  ref,
) {
  const reduce = useReducedMotion();
  const variants = motionSafe(V.dialog, reduce);

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
              transition={T.dialog}
            />
          </DialogPrimitive.Overlay>

          {/*
            Centring is done by this wrapper, not by a transform, and that is the whole point.

            The dialog used to sit at `left-1/2 top-1/2` and rely on `x: '-50%', y: '-50%'` in
            the motion props to pull itself back. But `V.dialog` animates a full `transform`
            string (`scale(0.97)`) for the WAAPI fast path, and when Framer Motion is handed
            both a `transform` string and `x`/`y`, the string wins — the translate was silently
            dropped, so every dialog in the app rendered with its top-left corner at the centre
            of the screen and hung off the bottom edge.

            Baking `translate(-50%, -50%)` into the string would fix the animated case and
            break the reduced-motion one, because `withoutTransform` strips `transform`
            entirely — those users would lose the centring instead.

            A flex wrapper is immune to both: position and animation stop sharing a channel.
            `pointer-events-none` here keeps click-outside-to-close working on the scrim
            underneath, and the panel re-enables them for itself.
          */}
          <div className="pointer-events-none fixed inset-0 z-50 flex items-center justify-center p-6">
            <DialogPrimitive.Content ref={ref} asChild forceMount {...props}>
              <motion.div
                className={cn(
                  'accent-hairline pointer-events-auto flex max-h-[80vh] w-full flex-col overflow-hidden',
                  'rounded-xl border border-strong bg-overlay shadow-dialog',
                  className,
                )}
                style={{ maxWidth: WIDTHS[size], transformOrigin: 'center' }}
                initial={variants.initial}
                animate={variants.animate}
                exit={variants.exit}
                transition={T.dialog}
              >
                <div className="flex items-start justify-between gap-3 p-5 pb-3">
                  <div className="min-w-0">
                    <DialogPrimitive.Title className="font-display text-md font-semibold text-primary">
                      {title}
                    </DialogPrimitive.Title>
                    {description !== undefined && (
                      <DialogPrimitive.Description className="mt-1 text-sm text-secondary">
                        {description}
                      </DialogPrimitive.Description>
                    )}
                  </div>
                  {!hideClose && (
                    <DialogPrimitive.Close asChild>
                      <Button variant="ghost" size="sm" icon aria-label="Close">
                        <X aria-hidden="true" />
                      </Button>
                    </DialogPrimitive.Close>
                  )}
                </div>

                <div className="scroll-region min-h-0 flex-1 px-5 pb-5">{children}</div>

                {footer !== undefined && (
                  <div className="flex items-center justify-end gap-2 border-t border-state-divider p-4">
                    {footer}
                  </div>
                )}
              </motion.div>
            </DialogPrimitive.Content>
          </div>
        </DialogPrimitive.Portal>
      )}
    </AnimatePresence>
  );
});

/** Props for {@link ConfirmDialog}. */
export interface ConfirmDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  /** Say what will happen, in the user's terms. Never "Are you sure?" alone. */
  description: ReactNode;
  /** Title case, verb + noun: `Archive Application`, `Stop the Run`. */
  confirmLabel: string;
  onConfirm: () => void;
  /** Renders the confirm button as `danger`. True for anything irreversible. */
  destructive?: boolean;
  loading?: boolean;
}

/**
 * The confirmation every `Ctrl`-namespaced action must show.
 *
 * §9.1 makes `Ctrl` mean irreversible, and §9.5 requires `Ctrl+X` and `Ctrl+⇧X` to confirm.
 * This is that dialog, so the copy and the button tier cannot drift between the six places
 * that need it.
 */
export function ConfirmDialog({
  open,
  onOpenChange,
  title,
  description,
  confirmLabel,
  onConfirm,
  destructive = true,
  loading = false,
}: ConfirmDialogProps) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        open={open}
        size="sm"
        title={title}
        description={description}
        footer={
          <>
            <DialogClose asChild>
              <Button variant="secondary">Cancel</Button>
            </DialogClose>
            <Button
              variant={destructive ? 'danger' : 'primary'}
              loading={loading}
              onClick={onConfirm}
            >
              {confirmLabel}
            </Button>
          </>
        }
      >
        {null}
      </DialogContent>
    </Dialog>
  );
}
