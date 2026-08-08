/**
 * Page chrome: the 56px header, the 44px toolbar, and the scrolling content region
 * (`docs/UI.md` §5.4, §5.5, §5.6).
 *
 * Three rules are implemented here once rather than in each of the twelve screens:
 *
 * **The header rule appears only once the content has scrolled.** A permanent rule under a
 * page title is a border for its own sake; a rule that appears when content passes beneath it
 * is information. The scroll state lives in this component because the content region is the
 * scroll container, and cross-fading over 140ms is what stops it reading as a flicker.
 *
 * **Route change moves focus to the `<h1>`** (§9.7). It is the only focus jump in the app, and
 * it is what lets a keyboard user press `g a` and then `Tab` into the applications table
 * rather than into the sidebar they just left.
 *
 * **The 2px `IndeterminateBar` is pinned to the toolbar's bottom edge** and is the *only*
 * affordance a background refetch gets (§10.1). It is never an overlay and never replaces
 * content, which is why `busy` is a prop on the chrome rather than a condition in a screen.
 */

import { useRouterState } from '@tanstack/react-router';
import { X } from 'lucide-react';
import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type ReactNode,
  type UIEvent,
} from 'react';

import { Button, IndeterminateBar } from '@/components/ui';
import { cn } from '@/lib/utils';

/** Props for {@link Page}. */
export interface PageProps {
  /** `--text-xl`, weight 590. The entity's identity on a detail route. */
  title: string;
  /** One line of live counts. Always `tabular-nums`, so a tick never reflows it. */
  subtitle?: ReactNode;
  /** Right-aligned: at most one primary plus two secondary or icon buttons. */
  actions?: ReactNode;
  /** The 44px filter bar. Omitted on screens that do not filter. */
  toolbar?: ReactNode;
  /**
   * Whether a background refetch is in flight.
   *
   * Drives the 2px bar and nothing else. Never gate content on this — §10.1 makes `isPending`
   * the only value permitted to do that.
   */
  busy?: boolean;
  /**
   * Let the content region own its own layout instead of receiving the standard
   * `max-width: 1440px` reading column. Tables and three-pane screens set this, per §5.6:
   * a 1440px cap on a wide data grid wastes a widescreen monitor.
   */
  bleed?: boolean;
  /** Cap the content at the 760px measure. Settings, onboarding and detail bodies do. */
  measure?: boolean;
  children: ReactNode;
}

/** The standard screen template. */
export function Page({
  title,
  subtitle,
  actions,
  toolbar,
  busy = false,
  bleed = false,
  measure = false,
  children,
}: PageProps) {
  const pathname = useRouterState({ select: (state) => state.location.pathname });
  const heading = useRef<HTMLHeadingElement | null>(null);
  const [scrolled, setScrolled] = useState(false);

  // §9.7 — the one sanctioned focus jump. `preventScroll` because the header is sticky and
  // the browser's default scroll-into-view would fight the scroll position we just restored.
  useEffect(() => {
    heading.current?.focus({ preventScroll: true });
    setScrolled(false);
  }, [pathname]);

  const onScroll = useCallback((event: UIEvent<HTMLDivElement>) => {
    setScrolled(event.currentTarget.scrollTop > 0);
  }, []);

  return (
    <div className="flex min-h-0 flex-1 flex-col bg-page">
      <header
        className={cn(
          'relative z-20 flex h-14 shrink-0 items-center gap-4 bg-page px-6',
          'after:absolute after:inset-x-0 after:bottom-0 after:h-px after:bg-subtle',
          'after:transition-opacity after:duration-[140ms] after:ease-out-quad',
          scrolled ? 'after:opacity-100' : 'after:opacity-0',
        )}
      >
        <div className="min-w-0 flex-1">
          <h1
            ref={heading}
            tabIndex={-1}
            className="truncate font-display text-xl font-semibold text-primary outline-none"
          >
            {title}
          </h1>
          {subtitle !== undefined && (
            <p className="truncate text-sm tabular-nums text-muted">{subtitle}</p>
          )}
        </div>
        {actions !== undefined && (
          <div className="flex shrink-0 items-center gap-2">{actions}</div>
        )}
      </header>

      {toolbar !== undefined && (
        <div className="relative z-10 flex h-11 shrink-0 items-center gap-2 border-b border-subtle bg-page px-6">
          {toolbar}
          <IndeterminateBar
            active={busy}
            label="Refreshing"
            className="absolute inset-x-0 bottom-0"
          />
        </div>
      )}
      {toolbar === undefined && (
        <IndeterminateBar active={busy} label="Refreshing" className="shrink-0" />
      )}

      <div id="page-content" onScroll={onScroll} className="scroll-region min-h-0 flex-1">
        <div
          className={cn(
            'px-6 pb-12 pt-4',
            bleed ? 'h-full' : 'mx-auto w-full',
            !bleed && (measure ? 'max-w-[760px]' : 'max-w-[1440px]'),
          )}
        >
          {children}
        </div>
      </div>
    </div>
  );
}

/** Props for {@link FilterChip}. */
export interface FilterChipProps {
  label: string;
  /** Active chips take the accent wash and gain a clear affordance (§5.5). */
  active?: boolean;
  onClick?: () => void;
  /** Present only on an active chip. */
  onClear?: () => void;
}

/**
 * One filter chip.
 *
 * Chips, not selects: an active filter has to be legible from across the room, because the
 * commonest "where did my rows go" bug is a filter the user forgot they set.
 */
export function FilterChip({ label, active = false, onClick, onClear }: FilterChipProps) {
  return (
    <span className="inline-flex shrink-0 items-center">
      <Button
        variant="chip"
        size="sm"
        aria-pressed={active}
        onClick={onClick}
        className={cn(
          active && 'border-accent-border bg-accent-subtle text-accent-text',
          onClear !== undefined && active && 'rounded-r-none border-r-0 pr-1.5',
        )}
      >
        {label}
      </Button>
      {onClear !== undefined && active && (
        <Button
          variant="chip"
          size="sm"
          aria-label={`Clear ${label}`}
          onClick={onClear}
          className="rounded-l-none border-accent-border bg-accent-subtle pl-1 pr-1.5 text-accent-text"
        >
          <X className="size-3" aria-hidden="true" />
        </Button>
      )}
    </span>
  );
}

/** Pushes the toolbar's view controls to the right edge. */
export function ToolbarSpacer() {
  return <span className="flex-1" />;
}

/** Props for {@link ResultCount}. */
export interface ResultCountProps {
  count: number;
  total?: number;
  noun: string;
}

/** The mono result count that closes every toolbar. */
export function ResultCount({ count, total, noun }: ResultCountProps) {
  const shown = total === undefined || total === count ? String(count) : `${count} / ${total}`;
  return (
    <span className="shrink-0 font-mono text-mini tabular-nums text-muted">
      {shown} {noun}
    </span>
  );
}

/** Props for {@link SectionHeading}. */
export interface SectionHeadingProps {
  children: ReactNode;
  /** Right-aligned affordance — a count, a toggle, a link. */
  action?: ReactNode;
  className?: string;
}

/** The `.label-caps` eyebrow that separates regions inside a screen. */
export function SectionHeading({ children, action, className }: SectionHeadingProps) {
  return (
    <div className={cn('mb-2 flex h-6 items-center justify-between gap-3', className)}>
      <h2 className="label-caps">{children}</h2>
      {action}
    </div>
  );
}
