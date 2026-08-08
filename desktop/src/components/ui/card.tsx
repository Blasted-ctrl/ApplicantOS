/**
 * Card — `shadcn+` (`docs/UI.md` §7.8).
 *
 * Level 2 of the elevation ladder: `--bg-surface` **and** `--border-default`, moved together
 * as one step. §4.4 makes that pairing the highest-leverage rule in the document — a
 * background change without its border change does not read as elevation, it reads as a
 * mistake.
 *
 * `--shadow-raised` is deliberately almost invisible. The part doing the work is the
 * `inset 0 1px 0 rgb(255 255 255 / .03)` top hairline inside it, which fakes a lit top edge;
 * on a dark field that inset is what sells a card as raised, and the drop shadow barely
 * registers.
 *
 * Two usage rules the component enforces: a card **must have a title**, and a card is **never
 * nested inside a card** — use a divider and a `.label-caps` group heading instead. The first
 * is why `CardHeader` takes `title` as a required prop rather than as children.
 */

import type { ReactNode } from 'react';

import { cn } from '@/lib/utils';

/** Props for {@link Card}. */
export interface CardProps extends React.ComponentPropsWithoutRef<'div'> {
  /** Adds `.lift`: `translateY(-1px)` and a border step on hover. Cards only, never rows. */
  interactive?: boolean;
  /**
   * Structural selection: `--bg-elevated` + `--border-strong` + an accent glow.
   *
   * Not the same thing as hover. Hover applies a transient alpha layer; selection is a real
   * elevation step, and conflating them is what makes a list feel like it is flickering.
   */
  selected?: boolean;
  /** Drops `.lift` and `.pressable` so the card structurally cannot respond. */
  disabled?: boolean;
  /** 20px interior instead of 16px, for a detail panel rather than a grid tile. */
  padded?: 'compact' | 'detail';
}

/** A surface. */
export function Card({
  interactive = false,
  selected = false,
  disabled = false,
  padded = 'compact',
  className,
  children,
  ...props
}: CardProps) {
  return (
    <div
      data-disabled={disabled ? 'true' : undefined}
      data-selected={selected ? 'true' : undefined}
      className={cn(
        'relative rounded-lg border bg-surface shadow-raised',
        padded === 'detail' ? 'p-5' : 'p-4',
        selected ? 'border-strong bg-elevated shadow-selected' : 'border-default',
        interactive && !disabled && 'lift cursor-pointer',
        disabled && 'cursor-not-allowed border-subtle opacity-60',
        className,
      )}
      {...props}
    >
      {children}
    </div>
  );
}

/** Props for {@link CardHeader}. */
export interface CardHeaderProps extends Omit<React.ComponentPropsWithoutRef<'div'>, 'title'> {
  /** Required: §7.8 — a card must have a title. */
  title: ReactNode;
  /** Sentence case, and it must carry *new* information — never a restatement (§3.7). */
  subtitle?: ReactNode;
  /** Right-aligned actions. At most one primary. */
  actions?: ReactNode;
}

/** Title (15px / 510), optional subtitle (13px muted), optional right-aligned actions. */
export function CardHeader({
  title,
  subtitle,
  actions,
  className,
  children,
  ...props
}: CardHeaderProps) {
  return (
    <div className={cn('flex items-start justify-between gap-3', className)} {...props}>
      <div className="min-w-0">
        <h3 className="truncate font-display text-md font-medium text-primary">{title}</h3>
        {subtitle !== undefined && <p className="mt-0.5 text-sm text-muted">{subtitle}</p>}
        {children}
      </div>
      {actions !== undefined && (
        <div className="flex shrink-0 items-center gap-2">{actions}</div>
      )}
    </div>
  );
}

/** The card's body. 12px below the header, per §4.1. */
export function CardBody({ className, ...props }: React.ComponentPropsWithoutRef<'div'>) {
  return <div className={cn('mt-3', className)} {...props} />;
}

/**
 * Metadata footer: a `--state-divider` rule, 12px of padding, mono at 11px.
 *
 * The divider is one step *below* `--border-subtle` so an internal rule never competes with
 * the card's own edge (§4.5).
 */
export function CardFooter({ className, ...props }: React.ComponentPropsWithoutRef<'div'>) {
  return (
    <div
      className={cn(
        'mt-3 flex items-center gap-3 border-t border-state-divider pt-3',
        'font-mono text-micro tracking-normal text-muted',
        className,
      )}
      {...props}
    />
  );
}

/**
 * A caps eyebrow for a group inside a card.
 *
 * §7.8 forbids nesting a card in a card; this plus a divider is the replacement, and
 * `text-transform: uppercase` has exactly this one use in the product (§3.6).
 */
export function CardGroupLabel({ className, ...props }: React.ComponentPropsWithoutRef<'div'>) {
  return <div className={cn('label-caps flex h-6 items-center', className)} {...props} />;
}
