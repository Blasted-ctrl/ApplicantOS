/**
 * Checkbox, Radio and Switch — `shadcn` (Radix), re-skinned (`docs/UI.md` §7.5, §4.2).
 *
 * 16×16 (`sm` 14), `--radius-xs` for the checkbox and `full` for the radio — the shape is the
 * only difference the user needs, and it is the one platforms have taught them.
 *
 * One detail that is easy to get wrong: **the glyph does not animate in.** Background and
 * border transition over 140ms, but the check mark itself appears on the same frame as the
 * click. A checkbox is toggled dozens of times a session (§6.4), and 150ms of check-mark
 * animation is 150ms of the user wondering whether the click registered.
 *
 * The whole label-plus-control is one 24px click target: a 16px hit area is a miss waiting to
 * happen, and the label is the part people aim at anyway.
 */

import * as CheckboxPrimitive from '@radix-ui/react-checkbox';
import * as RadioGroupPrimitive from '@radix-ui/react-radio-group';
import * as SwitchPrimitive from '@radix-ui/react-switch';
import { Check, Minus } from 'lucide-react';
import { forwardRef, type ReactNode } from 'react';

import { cn } from '@/lib/utils';

/** Shared box treatment for the checkbox and the radio. */
const CONTROL_BASE = cn(
  'peer inline-flex shrink-0 items-center justify-center border bg-inset',
  'border-strong text-white',
  'transition-[background-color,border-color] duration-[140ms] ease-out-quad',
  'hover:border-accent-border',
  'data-[state=checked]:border-accent data-[state=checked]:bg-accent',
  'data-[state=indeterminate]:border-accent data-[state=indeterminate]:bg-accent',
  'disabled:cursor-not-allowed disabled:border-subtle disabled:bg-surface disabled:text-disabled',
);

/** Props for {@link Checkbox}. */
export interface CheckboxProps
  extends React.ComponentPropsWithoutRef<typeof CheckboxPrimitive.Root> {
  size?: 'sm' | 'md';
}

/** A checkbox. */
export const Checkbox = forwardRef<
  React.ComponentRef<typeof CheckboxPrimitive.Root>,
  CheckboxProps
>(function Checkbox({ className, size = 'md', ...props }, ref) {
  return (
    <CheckboxPrimitive.Root
      ref={ref}
      className={cn(
        CONTROL_BASE,
        'rounded-xs',
        size === 'sm' ? 'size-3.5' : 'size-4',
        className,
      )}
      {...props}
    >
      <CheckboxPrimitive.Indicator className="inline-flex items-center justify-center">
        {props.checked === 'indeterminate' ? (
          <Minus className="size-2.5" strokeWidth={3} aria-hidden="true" />
        ) : (
          <Check className="size-2.5" strokeWidth={3} aria-hidden="true" />
        )}
      </CheckboxPrimitive.Indicator>
    </CheckboxPrimitive.Root>
  );
});

/** Props for {@link CheckboxField}. */
export interface CheckboxFieldProps extends CheckboxProps {
  label: ReactNode;
  /** Help text below the label. */
  description?: ReactNode;
}

/** A checkbox with its label, as one 24px-tall click target. */
export const CheckboxField = forwardRef<
  React.ComponentRef<typeof CheckboxPrimitive.Root>,
  CheckboxFieldProps
>(function CheckboxField({ label, description, className, id, ...props }, ref) {
  const controlId = id ?? `checkbox-${String(label)}`;
  return (
    <div className={cn('flex min-h-6 items-start gap-2', className)}>
      <span className="flex h-6 items-center">
        <Checkbox ref={ref} id={controlId} {...props} />
      </span>
      <label htmlFor={controlId} className="cursor-pointer select-none py-0.5">
        <span className="block text-base text-primary">{label}</span>
        {description !== undefined && (
          <span className="mt-0.5 block text-mini text-muted">{description}</span>
        )}
      </label>
    </div>
  );
});

/** Radio group root. */
export const RadioGroup = forwardRef<
  React.ComponentRef<typeof RadioGroupPrimitive.Root>,
  React.ComponentPropsWithoutRef<typeof RadioGroupPrimitive.Root>
>(function RadioGroup({ className, ...props }, ref) {
  return (
    <RadioGroupPrimitive.Root ref={ref} className={cn('flex flex-col gap-3', className)} {...props} />
  );
});

/** One radio. */
export const Radio = forwardRef<
  React.ComponentRef<typeof RadioGroupPrimitive.Item>,
  React.ComponentPropsWithoutRef<typeof RadioGroupPrimitive.Item>
>(function Radio({ className, ...props }, ref) {
  return (
    <RadioGroupPrimitive.Item
      ref={ref}
      className={cn(CONTROL_BASE, 'size-4 rounded-full', className)}
      {...props}
    >
      <RadioGroupPrimitive.Indicator className="inline-flex items-center justify-center">
        <span className="size-1.5 rounded-full bg-white" />
      </RadioGroupPrimitive.Indicator>
    </RadioGroupPrimitive.Item>
  );
});

/** Props for {@link RadioField}. */
export interface RadioFieldProps
  extends React.ComponentPropsWithoutRef<typeof RadioGroupPrimitive.Item> {
  label: ReactNode;
  description?: ReactNode;
}

/** A radio with its label, as one 24px-tall click target. */
export const RadioField = forwardRef<
  React.ComponentRef<typeof RadioGroupPrimitive.Item>,
  RadioFieldProps
>(function RadioField({ label, description, className, id, value, ...props }, ref) {
  const controlId = id ?? `radio-${value}`;
  return (
    <div className={cn('flex min-h-6 items-start gap-2', className)}>
      <span className="flex h-6 items-center">
        <Radio ref={ref} id={controlId} value={value} {...props} />
      </span>
      <label htmlFor={controlId} className="cursor-pointer select-none py-0.5">
        <span className="block text-base text-primary">{label}</span>
        {description !== undefined && (
          <span className="mt-0.5 block text-mini text-muted">{description}</span>
        )}
      </label>
    </div>
  );
});

/** Props for {@link Switch}. */
export interface SwitchProps
  extends React.ComponentPropsWithoutRef<typeof SwitchPrimitive.Root> {
  size?: 'sm' | 'md';
}

/**
 * A switch — 18×32 (`sm` 16×28), per §4.2.
 *
 * The knob is the one place `T.springSnap` applies outside a tab indicator: it is a single
 * small indicator whose motion is state-driven rather than duration-driven, which is exactly
 * what §6.3 reserves springs for.
 */
export const Switch = forwardRef<React.ComponentRef<typeof SwitchPrimitive.Root>, SwitchProps>(
  function Switch({ className, size = 'md', ...props }, ref) {
    const track = size === 'sm' ? 'h-4 w-7' : 'h-[18px] w-8';
    const knob = size === 'sm' ? 'size-3 data-[state=checked]:translate-x-3' : 'size-3.5 data-[state=checked]:translate-x-[14px]';
    return (
      <SwitchPrimitive.Root
        ref={ref}
        className={cn(
          'peer inline-flex shrink-0 cursor-pointer items-center rounded-full border border-strong bg-inset p-px',
          'transition-[background-color,border-color] duration-[140ms] ease-out-quad',
          'data-[state=checked]:border-accent data-[state=checked]:bg-accent',
          'disabled:cursor-not-allowed disabled:opacity-45',
          track,
          className,
        )}
        {...props}
      >
        <SwitchPrimitive.Thumb
          className={cn(
            'pointer-events-none block translate-x-0 rounded-full bg-primary shadow-raised',
            'transition-transform duration-[140ms] ease-out',
            'data-[state=checked]:bg-white',
            knob,
          )}
        />
      </SwitchPrimitive.Root>
    );
  },
);
