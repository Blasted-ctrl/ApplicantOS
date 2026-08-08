/**
 * Button — `shadcn` (Radix `Slot` for `asChild`), re-skinned to our tokens.
 *
 * `docs/UI.md` §7.1. Six variants, three heights, and one rule that governs all of them:
 * **solid accent appears once per view.** If two elements on screen are `background:
 * var(--accent)`, one of them is wrong — downgrade it to `outline-accent`, which is why that
 * variant exists at all.
 *
 * The loading state is the part worth reading. The label **stays in place**; only the leading
 * icon slot cross-fades to a spinner, the outgoing glyph blurring as it goes so the swap is
 * masked rather than snapped. The button's width is pinned to what it measured before the
 * transition, so a button with no leading icon does not grow by 20px mid-click and shove the
 * rest of the toolbar sideways. **Never replace the label with "Loading…"** — the user needs
 * to know what they pressed more than they need to know that it is happening.
 */

import { Slot } from '@radix-ui/react-slot';
import type { VariantProps } from 'class-variance-authority';
import { forwardRef, useEffect, useRef, useState, type ReactNode } from 'react';

import { cn } from '@/lib/utils';

import { buttonVariants } from './variants';

/** Props for {@link Button}. */
export interface ButtonProps
  extends Omit<React.ComponentPropsWithoutRef<'button'>, 'color'>,
    VariantProps<typeof buttonVariants> {
  /** Render as the child element, keeping the styling. Used for links that look like buttons. */
  asChild?: boolean;
  /** Cross-fade the leading icon to a spinner and mark the button busy. */
  loading?: boolean;
  /** 14px icon rendered before the label. Replaced by the spinner while loading. */
  leadingIcon?: ReactNode;
  /** 14px icon or `Kbd` chip rendered after the label. */
  trailingIcon?: ReactNode;
}

/** The 14px spinner shown in the leading slot while a mutation is in flight. */
function Spinner(): React.JSX.Element {
  return (
    <svg viewBox="0 0 16 16" fill="none" aria-hidden="true" className="size-3.5 animate-spin">
      <circle cx="8" cy="8" r="6.5" stroke="currentColor" strokeOpacity="0.25" strokeWidth="1.5" />
      <path
        d="M14.5 8A6.5 6.5 0 0 0 8 1.5"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
      />
    </svg>
  );
}

/**
 * A button.
 *
 * Every button has a verb (§7.1). Submit-type buttons inside a dialog bind `⌘Enter` and show
 * it as a trailing `Kbd`, which is the caller's job — this component only reserves the slot.
 */
export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  {
    className,
    variant,
    size,
    icon,
    asChild = false,
    loading = false,
    leadingIcon,
    trailingIcon,
    children,
    disabled,
    style,
    ...props
  },
  forwardedRef,
) {
  const localRef = useRef<HTMLButtonElement | null>(null);
  const [pinnedWidth, setPinnedWidth] = useState<number | null>(null);

  // Measure before the swap, not after: once the spinner is in the DOM the width has already
  // changed and pinning it would preserve the wrong number.
  useEffect(() => {
    if (loading) {
      setPinnedWidth((current) => current ?? localRef.current?.offsetWidth ?? null);
      return;
    }
    setPinnedWidth(null);
  }, [loading]);

  const Component = asChild ? Slot : 'button';
  const showSpinner = loading && !asChild;

  return (
    <Component
      ref={(node: HTMLButtonElement | null) => {
        localRef.current = node;
        if (typeof forwardedRef === 'function') forwardedRef(node);
        else if (forwardedRef !== null) forwardedRef.current = node;
      }}
      className={cn(buttonVariants({ variant, size, icon }), className)}
      disabled={disabled === true || loading}
      aria-busy={loading || undefined}
      style={pinnedWidth === null ? style : { ...style, width: pinnedWidth }}
      {...props}
    >
      {asChild ? (
        children
      ) : (
        <>
          {(leadingIcon !== undefined || showSpinner) && (
            <span className="relative inline-flex items-center justify-center">
              {/* The outgoing glyph stays mounted and blurs out, which is what masks the
                  swap. It also holds the slot's width open, so a button with no leading
                  icon does not resize when the spinner arrives. */}
              <span
                className={cn(
                  'inline-flex items-center justify-center transition-[opacity,filter] duration-200 ease-out',
                  showSpinner && 'opacity-0 blur-[2px]',
                )}
                aria-hidden={showSpinner || undefined}
              >
                {leadingIcon ?? <Spinner />}
              </span>
              {showSpinner && (
                <span className="absolute inset-0 inline-flex items-center justify-center">
                  <Spinner />
                </span>
              )}
            </span>
          )}
          {children}
          {trailingIcon !== undefined && (
            <span className="inline-flex items-center justify-center">{trailingIcon}</span>
          )}
        </>
      )}
    </Component>
  );
});
