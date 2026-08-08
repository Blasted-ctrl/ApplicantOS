/**
 * EmptyState — `custom` (`docs/UI.md` §7.17).
 *
 * **Two distinct empty states are mandatory per list, and both must be written.** They say
 * different things and a single shared one is always wrong for at least one of them:
 *
 * | Case | Title | Description | CTAs |
 * |---|---|---|---|
 * | Blank slate | `No applications yet` | Names the next action | one primary + one secondary |
 * | No results  | `No applications match "senior backend"` — **quotes the query verbatim** | `Clear the filter to see all 47.` | `Clear Filters`, nothing else |
 *
 * {@link NoResults} exists so the second one cannot be forgotten: it takes the query and the
 * unfiltered total and writes the copy itself.
 *
 * **Binding suppression rule:** the empty state does not render while `isPending` and the
 * search box is empty. That single condition kills the "No applications found" flash before
 * data lands, which is the most common reason a fast app feels broken. It lives in
 * `lib/utils.ts` as `shouldShowEmptyState`, so no screen has to remember it.
 */

import type { LucideIcon } from 'lucide-react';
import { Inbox } from 'lucide-react';
import type { ReactNode } from 'react';

import { cn } from '@/lib/utils';

import { Button } from './button';

/** Props for {@link EmptyState}. */
export interface EmptyStateProps extends React.ComponentPropsWithoutRef<'div'> {
  /** 20px, in a 40px `--bg-inset` circle. */
  icon?: LucideIcon;
  /** Title case, no trailing period. */
  title: string;
  /** Sentence case, carrying new information — never a restatement of the title. */
  description?: ReactNode;
  /** At most one primary and one secondary. Real buttons, so tab order and roles work. */
  primaryAction?: { label: string; onClick: () => void };
  secondaryAction?: { label: string; onClick: () => void };
}

/** The blank-slate and no-results surface. */
export function EmptyState({
  icon: Icon = Inbox,
  title,
  description,
  primaryAction,
  secondaryAction,
  className,
  ...props
}: EmptyStateProps) {
  return (
    <div
      className={cn('flex flex-col items-center gap-3 px-6 py-12 text-center', className)}
      {...props}
    >
      <span className="flex size-10 items-center justify-center rounded-full bg-inset text-muted">
        <Icon size={20} strokeWidth={1.75} aria-hidden="true" />
      </span>
      <h3 className="font-display text-md font-semibold text-primary">{title}</h3>
      {description !== undefined && (
        <p className="max-w-[42ch] text-sm text-secondary">{description}</p>
      )}
      {(primaryAction !== undefined || secondaryAction !== undefined) && (
        <div className="mt-1 flex items-center gap-2">
          {primaryAction !== undefined && (
            <Button variant="primary" onClick={primaryAction.onClick}>
              {primaryAction.label}
            </Button>
          )}
          {secondaryAction !== undefined && (
            <Button variant="secondary" onClick={secondaryAction.onClick}>
              {secondaryAction.label}
            </Button>
          )}
        </div>
      )}
    </div>
  );
}

/** Props for {@link NoResults}. */
export interface NoResultsProps {
  /** The noun, plural and lowercase: `applications`, `postings`, `facts`. */
  noun: string;
  /** The active query. Quoted verbatim in the title — the user must recognise their own words. */
  query?: string;
  /** How many rows exist without the filter. Named in the description so the offer is concrete. */
  totalWithoutFilters?: number;
  onClear: () => void;
}

/**
 * The *no results* state.
 *
 * Quoting the query is the whole point: "No applications found" leaves the user guessing which
 * of four active filters is responsible, while `No applications match "senior backend"` names
 * it. Only one action, and it is the one that fixes it.
 */
export function NoResults({ noun, query, totalWithoutFilters, onClear }: NoResultsProps) {
  const title =
    query === undefined || query === ''
      ? `No ${noun} match these filters`
      : `No ${noun} match "${query}"`;

  const description =
    totalWithoutFilters === undefined
      ? 'Clear the filters to see everything again.'
      : `Clear the filters to see all ${String(totalWithoutFilters)}.`;

  return (
    <EmptyState
      title={title}
      description={description}
      primaryAction={{ label: 'Clear Filters', onClick: onClear }}
    />
  );
}
