/**
 * Shared `cva` recipes for the control primitives.
 *
 * Split out of `button.tsx` and `input.tsx` for a mechanical reason rather than a conceptual
 * one: React Fast Refresh can only preserve state for a module whose exports are all
 * components, and a `cva()` call is a function value, not a constant. Keeping the recipes here
 * means an edit to a button's hover colour hot-reloads the button instead of remounting the
 * subtree that contains it.
 *
 * The pairing that matters: `fieldVariants` is shared by `Input` and by the `Select` trigger,
 * because §7.3 requires them to be *identical* — same heights, background, border and radius.
 * Two copies of those numbers would drift within a week.
 */

import { cva } from 'class-variance-authority';

import { cn } from '@/lib/utils';

/**
 * Button variants and sizes (`docs/UI.md` §7.1).
 *
 * `.pressable` supplies the 100ms `scale(0.97)` press. Hover is a **background-colour**
 * transition only: §2.7 rule 2 reserves movement for cards and buttons, and on a button the
 * press already is the movement.
 */
export const buttonVariants = cva(
  cn(
    'pressable relative inline-flex shrink-0 select-none items-center justify-center gap-1.5',
    'whitespace-nowrap rounded-md font-medium',
    'transition-[background-color,border-color,color,box-shadow,filter] duration-[140ms] ease-out-quad',
    'disabled:pointer-events-none disabled:opacity-45',
    '[&_svg]:pointer-events-none [&_svg]:shrink-0',
  ),
  {
    variants: {
      variant: {
        /** The only solid accent in a view. One per screen, and it is the primary action. */
        primary: cn(
          'bg-accent text-on-accent border-transparent',
          'hover:bg-accent-hover hover:brightness-[1.08] hover:shadow-accent',
          'active:bg-accent-active',
        ),
        /** The default for everything else. */
        secondary: cn(
          'border border-default bg-surface text-primary',
          'hover:border-strong hover:bg-elevated',
        ),
        /** Toolbars, table row actions, icon buttons. */
        ghost: cn(
          'border border-transparent text-secondary',
          'hover:bg-state-hover hover:text-primary',
        ),
        /** A second-tier accent action. Use this instead of a second primary. */
        'outline-accent': cn(
          'border border-accent-border bg-accent-subtle text-accent-text',
          'hover:bg-accent/[0.18]',
        ),
        /** Destructive. Never a solid red fill — a filled red button is a dare. */
        danger: cn(
          'border border-st-danger/[0.38] bg-st-danger/[0.12] text-st-danger',
          'hover:bg-st-danger/[0.18]',
        ),
        /** Filters only. Shape separates the tier: a chip is a pill, a button is not. */
        chip: cn(
          'rounded-full border border-default bg-surface text-secondary',
          'hover:bg-state-hover',
        ),
      },
      size: {
        sm: 'h-[26px] px-2 text-sm [&_svg]:size-3.5',
        md: 'h-[30px] px-2.5 text-base [&_svg]:size-3.5',
        lg: 'h-[34px] px-3.5 text-base [&_svg]:size-4',
      },
      /** Square, with the icon optically centred. */
      icon: {
        true: 'px-0',
        false: '',
      },
    },
    compoundVariants: [
      { icon: true, size: 'sm', class: 'w-[26px]' },
      { icon: true, size: 'md', class: 'w-[30px]' },
      { icon: true, size: 'lg', class: 'w-[34px]' },
      // A filter chip is 24px tall regardless of the size scale — it is a different object.
      { variant: 'chip', size: 'sm', class: 'h-6 text-mini' },
    ],
    defaultVariants: { variant: 'secondary', size: 'md', icon: false },
  },
);

/**
 * Height, padding and type scale for `Input` and the `Select` trigger (§7.2, §7.3).
 *
 * The background is `--bg-inset`, **recessed below** the card the field sits in. Elevation
 * here is a background-and-border pair, so a control that is *lower* than its container reads
 * as something you put things into rather than something you press.
 */
export const fieldVariants = cva(
  cn(
    'flex w-full items-center rounded-md border border-default bg-inset text-primary',
    'transition-[border-color,background-color] duration-[140ms] ease-out-quad',
    'placeholder:text-muted',
    'hover:border-strong',
    'focus-visible:border-accent-border',
    'disabled:cursor-not-allowed disabled:border-subtle disabled:bg-surface disabled:text-disabled',
    'aria-[invalid=true]:border-st-danger/50',
  ),
  {
    variants: {
      size: {
        sm: 'h-[26px] px-2 text-sm',
        md: 'h-[30px] px-2 text-base',
        lg: 'h-[34px] px-2.5 text-base',
      },
      /**
       * Mono for ids, URLs, numbers and paths (§3.5). Not a style choice: the mono face is
       * doing semantics as well as alignment, and a UUID in a proportional face is unreadable.
       */
      mono: { true: 'font-mono', false: '' },
    },
    defaultVariants: { size: 'md', mono: false },
  },
);
