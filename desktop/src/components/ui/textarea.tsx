/**
 * Textarea — `shadcn`, re-skinned (`docs/UI.md` §7.4).
 *
 * Minimum height 66px (three rows at 13px), `resize: vertical` only — horizontal resize would
 * break the column it sits in and there is never a reason for it.
 *
 * The character counter turns `--st-review` at 90% of `max_length` and `--st-danger` at 100%.
 * That matters more here than it would elsewhere: essay answers in the review queue have a
 * real server-side cap, and `ReviewReason.TOO_MANY_ESSAYS` exists because overflowing one is
 * a reason to stop and ask rather than to truncate.
 *
 * The auto-grow variant is the **second and last** sanctioned height animation in the product
 * (the first is the accordion). It grows to twelve rows and then scrolls, using `T.resize`.
 */

import { forwardRef, useCallback, useEffect, useImperativeHandle, useRef } from 'react';

import { cn } from '@/lib/utils';

/** Minimum height: three rows at 13px with 1.5 line-height, plus the vertical padding. */
const MIN_HEIGHT_PX = 66;

/** Row height used to convert the twelve-row auto-grow cap into pixels. */
const ROW_HEIGHT_PX = 20;

/** Rows the auto-grow variant reaches before it starts scrolling. */
const MAX_ROWS = 12;

/** Props for {@link Textarea}. */
export interface TextareaProps extends React.ComponentPropsWithoutRef<'textarea'> {
  /**
   * Grow with the content up to twelve rows, then scroll.
   *
   * Height animation is normally banned (§6.4); this and the accordion are the two exceptions,
   * and both are documented in `docs/UI.md` by name.
   */
  autoGrow?: boolean;
}

/** A multi-line text field. */
export const Textarea = forwardRef<HTMLTextAreaElement, TextareaProps>(function Textarea(
  { className, autoGrow = false, onChange, style, ...props },
  forwardedRef,
) {
  const innerRef = useRef<HTMLTextAreaElement | null>(null);
  useImperativeHandle(forwardedRef, () => innerRef.current as HTMLTextAreaElement, []);

  const resize = useCallback(() => {
    const node = innerRef.current;
    if (node === null || !autoGrow) return;
    // Collapse first: `scrollHeight` on an already-grown element reports the grown height,
    // so without this the field can only ever get taller.
    node.style.height = 'auto';
    const max = MAX_ROWS * ROW_HEIGHT_PX;
    node.style.height = `${String(Math.min(Math.max(node.scrollHeight, MIN_HEIGHT_PX), max))}px`;
    node.style.overflowY = node.scrollHeight > max ? 'auto' : 'hidden';
  }, [autoGrow]);

  useEffect(resize, [resize, props.value, props.defaultValue]);

  return (
    <textarea
      ref={innerRef}
      onChange={(event) => {
        resize();
        onChange?.(event);
      }}
      className={cn(
        'w-full rounded-md border border-default bg-inset px-2 py-1.5',
        'text-sm/[1.5] text-primary placeholder:text-muted',
        'transition-[border-color,height] duration-[140ms] ease-out-quad',
        'hover:border-strong focus-visible:border-accent-border',
        'disabled:cursor-not-allowed disabled:border-subtle disabled:bg-surface disabled:text-disabled',
        'aria-[invalid=true]:border-st-danger/50',
        autoGrow ? 'resize-none' : 'resize-y',
        className,
      )}
      style={{
        minHeight: MIN_HEIGHT_PX,
        ...(autoGrow ? { transition: 'height 300ms var(--ease-drawer)' } : {}),
        ...style,
      }}
      {...props}
    />
  );
});

/** Props for {@link CharacterCounter}. */
export interface CharacterCounterProps extends React.ComponentPropsWithoutRef<'span'> {
  value: string;
  /** The server-side cap. `ReviewField.max_length` where one is known. */
  max: number;
}

/**
 * The bottom-right character counter.
 *
 * Warns at 90% and goes danger at 100% — early enough to rewrite rather than to discover the
 * limit by being truncated.
 */
export function CharacterCounter({ value, max, className, ...props }: CharacterCounterProps) {
  const used = value.length;
  const ratio = max === 0 ? 0 : used / max;
  const tone = ratio >= 1 ? 'text-st-danger' : ratio >= 0.9 ? 'text-st-review' : 'text-muted';

  return (
    <span
      className={cn('block text-right font-mono text-micro tabular-nums', tone, className)}
      aria-live={ratio >= 0.9 ? 'polite' : 'off'}
      {...props}
    >
      {used}/{max}
    </span>
  );
}
