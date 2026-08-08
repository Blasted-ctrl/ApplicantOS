/**
 * Input, Label, HelpText, FieldError, and the search variant — `shadcn`, re-skinned.
 *
 * `docs/UI.md` §7.2. The one detail that makes a field read as a field: its background is
 * `--bg-inset`, **recessed below** the card it sits in. Elevation in this product is a
 * background-and-border pair, so a control that is *lower* than its container reads as
 * something you put things into rather than something you press.
 *
 * The search variant animates its width from 240px to 320px on focus. That is the single
 * documented exception to the no-width-animation rule (§7.2), and it is allowed only because
 * a search input is one isolated element in a toolbar with no siblings that would reflow.
 * Do not copy the technique anywhere else.
 */

import type { VariantProps } from 'class-variance-authority';
import { Search } from 'lucide-react';
import { forwardRef, type ReactNode } from 'react';

import { cn } from '@/lib/utils';

import { Kbd } from './kbd';
import { fieldVariants } from './variants';

/** Props for {@link Input}. */
export interface InputProps
  extends Omit<React.ComponentPropsWithoutRef<'input'>, 'size'>,
    VariantProps<typeof fieldVariants> {
  /** 14px icon rendered inside the field, before the value. */
  leadingIcon?: ReactNode;
  /** Rendered inside the field, after the value — a unit, a clear button, a keycap. */
  trailing?: ReactNode;
}

/** A text field. */
export const Input = forwardRef<HTMLInputElement, InputProps>(function Input(
  { className, size, mono, leadingIcon, trailing, ...props },
  ref,
) {
  if (leadingIcon === undefined && trailing === undefined) {
    return <input ref={ref} className={cn(fieldVariants({ size, mono }), className)} {...props} />;
  }

  // With an affordance in the field, the border and background move to the wrapper so the
  // focus ring surrounds the whole control rather than just the text box inside it.
  return (
    <div
      className={cn(
        fieldVariants({ size, mono }),
        'gap-1.5 focus-within:border-accent-border',
        className,
      )}
      data-disabled={props.disabled === true ? 'true' : undefined}
    >
      {leadingIcon !== undefined && (
        <span className="inline-flex shrink-0 items-center text-muted [&_svg]:size-3.5">
          {leadingIcon}
        </span>
      )}
      <input
        ref={ref}
        className="min-w-0 flex-1 bg-transparent outline-none placeholder:text-muted disabled:text-disabled"
        {...props}
      />
      {trailing !== undefined && <span className="inline-flex shrink-0 items-center">{trailing}</span>}
    </div>
  );
});

/** Props for {@link SearchInput}. */
export interface SearchInputProps extends Omit<InputProps, 'leadingIcon' | 'trailing'> {
  /** Whether to show the `Esc` keycap. True when the field is focused and non-empty. */
  showEscape?: boolean;
}

/**
 * The toolbar search field.
 *
 * 240px, growing to 320px on focus over `--dur-2` with `ease-out-quad` — the one sanctioned
 * width animation in the product.
 */
export const SearchInput = forwardRef<HTMLInputElement, SearchInputProps>(function SearchInput(
  { className, showEscape = false, ...props },
  ref,
) {
  return (
    <Input
      ref={ref}
      type="search"
      size="sm"
      leadingIcon={<Search aria-hidden="true" />}
      trailing={showEscape ? <Kbd keys={['Esc']} /> : undefined}
      className={cn(
        'w-[240px] transition-[width,border-color,background-color] duration-[140ms] ease-out-quad',
        'focus-within:w-[320px]',
        className,
      )}
      {...props}
    />
  );
});

/** Props for {@link Label}. */
export interface LabelProps extends React.ComponentPropsWithoutRef<'label'> {
  /** Adds the required marker. Never a bare asterisk — it is announced as "required". */
  required?: boolean;
}

/** A field label: 12px, weight 510, `--fg-secondary`, 6px above its control. */
export function Label({ className, required = false, children, ...props }: LabelProps) {
  return (
    <label className={cn('block text-mini font-medium text-secondary', className)} {...props}>
      {children}
      {required && (
        <span className="ml-0.5 text-st-danger" aria-hidden="true">
          *
        </span>
      )}
      {required && <span className="sr-only"> (required)</span>}
    </label>
  );
}

/** Help text below a field. Replaced by {@link FieldError}, never shown alongside it. */
export function HelpText({ className, ...props }: React.ComponentPropsWithoutRef<'p'>) {
  return <p className={cn('text-mini text-muted', className)} {...props} />;
}

/**
 * A validation message.
 *
 * `role="alert"` so it is announced when it appears, and the caller must also set
 * `aria-invalid` on the control — colour is never the only channel (§12.2).
 */
export function FieldError({ className, ...props }: React.ComponentPropsWithoutRef<'p'>) {
  return <p role="alert" className={cn('text-mini text-st-danger', className)} {...props} />;
}

/** Props for {@link Field}. */
export interface FieldProps extends React.ComponentPropsWithoutRef<'div'> {
  label?: ReactNode;
  help?: ReactNode;
  /** When present, replaces `help` — never both (§7.2). */
  error?: ReactNode;
  required?: boolean;
  htmlFor?: string;
}

/** Label + control + help/error, with the 6px and 12px gaps from §4.1. */
export function Field({
  label,
  help,
  error,
  required = false,
  htmlFor,
  className,
  children,
  ...props
}: FieldProps) {
  return (
    <div className={cn('flex flex-col gap-1.5', className)} {...props}>
      {label !== undefined && (
        <Label htmlFor={htmlFor} required={required}>
          {label}
        </Label>
      )}
      {children}
      {error !== undefined ? (
        <FieldError>{error}</FieldError>
      ) : help !== undefined ? (
        <HelpText>{help}</HelpText>
      ) : null}
    </div>
  );
}
