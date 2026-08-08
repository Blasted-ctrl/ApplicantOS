/**
 * DataTable — `custom` (TanStack Virtual over `role="grid"` divs). `docs/UI.md` §7.10.
 *
 * **Not shadcn's `Table`.** That is a styled `<table>`; this needs virtualisation, column
 * pinning and 5 000-row scroll, none of which a semantic table gives without fighting it.
 * The `role="grid"` / `role="row"` / `role="gridcell"` semantics are supplied explicitly so
 * the accessibility tree is the same one a `<table>` would have produced.
 *
 * Binding virtualiser rules from §10.11, all implemented here:
 *
 * - **Fixed row heights. Never `measureElement`.** Dynamic measurement attaches a
 *   `ResizeObserver` per row and forces layout reads every frame; it is the single most
 *   common cause of a sub-60fps virtualised table.
 * - **One `transform: translateY()` on a wrapper**, not per-row absolute positioning. Fifty
 *   absolutely positioned rows are fifty layout participants.
 * - **`getItemKey` returns the entity id**, never the index, so React reuses row DOM when a
 *   row is prepended by a live event instead of rebuilding the whole window.
 * - **`overscan: 8`.** The default of 1 shows blank strips on fast trackpad scroll; 8 rows at
 *   44px is about 350px of runway.
 * - **No Framer Motion on rows. Ever.** `AnimatePresence` and virtualisation actively fight:
 *   AP keeps exiting nodes mounted while the virtualiser wants them gone, at the cost of a
 *   forced layout per row.
 *
 * Visual rules that are easy to lose: **no row separators by default** (rows separate by
 * hover fill and spacing — borders-off is the premium tell), **no zebra striping, ever**, and
 * hover is a **colour change only**, never a transform.
 */

import { useVirtualizer } from '@tanstack/react-virtual';
import { ChevronDown, ChevronUp } from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, type ReactNode } from 'react';

import { usePrefetch, type PrefetchableOptions } from '@/hooks/use-prefetch';
import { cn } from '@/lib/utils';

import { Checkbox } from './checkbox';

/** Row count past which the body virtualises (§10.11). */
export const VIRTUALIZE_THRESHOLD = 100;

/** Rows rendered beyond the visible window on each side. */
const OVERSCAN = 8;

/** One column. */
export interface DataTableColumn<T> {
  id: string;
  /** Title-case noun phrase — `Last activity`, not `LAST ACTIVITY` (§3.6). */
  header: string;
  /** A CSS grid track: `1fr`, `minmax(0, 2fr)`, `120px`. */
  width: string;
  /** Right-align numbers; everything else is left-aligned. */
  align?: 'left' | 'right';
  /** Mono at 12px in `--fg-secondary`: ids, numbers, dates, durations, URLs (§3.5). */
  mono?: boolean;
  cell: (row: T, index: number) => ReactNode;
  /** Adds a sort affordance to the header. The caller owns the sort state. */
  sortable?: boolean;
}

/** Sort state, owned by the caller. */
export interface SortState {
  columnId: string;
  direction: 'asc' | 'desc';
}

/** Props for {@link DataTable}. */
export interface DataTableProps<T extends { id: string }> {
  rows: readonly T[];
  columns: readonly DataTableColumn<T>[];
  /** Announced name for the grid. Required — an unnamed grid is an unnavigable one. */
  label: string;
  /** From the density setting (§4.2): 30 / 36 / 44. */
  rowHeight?: number;
  /**
   * Rows the page is sized for.
   *
   * The body carries `min-height: rowHeight × pageSize`, so a twelve-row page and a fifty-row
   * page occupy the same box and paging never shifts the layout (§10.13).
   */
  pageSize?: number;
  /** Total rows matching the query, for `aria-rowcount`. Defaults to the rendered count. */
  totalCount?: number;
  /** The keyboard-focused row, or `-1`. Roving tabindex: only this row is tabbable. */
  focusedIndex?: number;
  onFocusRow?: (index: number) => void;
  /** `↵` on the focused row, and a click. */
  onOpenRow?: (row: T, index: number) => void;
  /** Selected ids. Presence of any selection reveals every row's checkbox. */
  selectedIds?: ReadonlySet<string>;
  onToggleSelect?: (row: T) => void;
  /** Row actions — a `⋯` ghost button, rendered only on hover or focus. */
  rowActions?: (row: T) => ReactNode;
  /** Warms a row's detail query on hover and on focus (§10.6). */
  prefetchOptions?: (row: T) => PrefetchableOptions | null;
  sort?: SortState;
  onSortChange?: (columnId: string) => void;
  /** Rendered instead of rows. Never a skeleton — §7.16 forbids it (`EmptyState` instead). */
  empty?: ReactNode;
  /**
   * Whether the rows are `placeholderData` from the previous filter.
   *
   * Drives `opacity: 0.65; pointer-events: none` with a 120ms transition — never a spinner,
   * never an empty table (§10.12).
   */
  isPlaceholder?: boolean;
  className?: string;
}

/** A dense, virtualised data grid. */
export function DataTable<T extends { id: string }>({
  rows,
  columns,
  label,
  rowHeight = 36,
  pageSize = 50,
  totalCount,
  focusedIndex = -1,
  onFocusRow,
  onOpenRow,
  selectedIds,
  onToggleSelect,
  rowActions,
  prefetchOptions,
  sort,
  onSortChange,
  empty,
  isPlaceholder = false,
  className,
}: DataTableProps<T>) {
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const selectable = onToggleSelect !== undefined;
  const anySelected = selectedIds !== undefined && selectedIds.size > 0;

  const template = useMemo(() => {
    const leading = selectable ? '32px ' : '';
    const trailing = rowActions === undefined ? '' : ' 36px';
    return `${leading}${columns.map((column) => column.width).join(' ')}${trailing}`;
  }, [columns, selectable, rowActions]);

  const virtualize = rows.length > VIRTUALIZE_THRESHOLD;

  const virtualizer = useVirtualizer({
    count: rows.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => rowHeight,
    overscan: OVERSCAN,
    getItemKey: (index) => rows[index]?.id ?? index,
    enabled: virtualize,
  });

  // Arrow keys move focus, never the scroll position; the virtualiser brings the focused row
  // into view with `block: 'nearest'` so a `j` at the bottom edge scrolls by one row rather
  // than centring it (§9.4).
  useEffect(() => {
    if (focusedIndex < 0) return;
    if (virtualize) {
      virtualizer.scrollToIndex(focusedIndex, { align: 'auto' });
      return;
    }
    const node = scrollRef.current?.querySelector<HTMLElement>(
      `[data-row-index="${String(focusedIndex)}"]`,
    );
    node?.scrollIntoView({ block: 'nearest' });
  }, [focusedIndex, virtualize, virtualizer]);

  const renderRow = useCallback(
    (row: T, index: number, style?: React.CSSProperties) => (
      <TableRow
        key={row.id}
        row={row}
        index={index}
        columns={columns}
        template={template}
        rowHeight={rowHeight}
        focused={index === focusedIndex}
        selected={selectedIds?.has(row.id) ?? false}
        showCheckbox={selectable && anySelected}
        selectable={selectable}
        onFocusRow={onFocusRow}
        onOpenRow={onOpenRow}
        onToggleSelect={onToggleSelect}
        rowActions={rowActions}
        prefetchOptions={prefetchOptions}
        style={style}
      />
    ),
    [
      columns,
      template,
      rowHeight,
      focusedIndex,
      selectedIds,
      selectable,
      anySelected,
      onFocusRow,
      onOpenRow,
      onToggleSelect,
      rowActions,
      prefetchOptions,
    ],
  );

  const virtualItems = virtualizer.getVirtualItems();
  const offsetY = virtualItems[0]?.start ?? 0;

  return (
    <div
      className={cn(
        'flex min-h-0 flex-col overflow-hidden rounded-lg border border-default bg-surface',
        'transition-opacity duration-[120ms] ease-out-quad',
        isPlaceholder && 'pointer-events-none opacity-65',
        className,
      )}
    >
      <div
        ref={scrollRef}
        role="grid"
        aria-label={label}
        aria-rowcount={totalCount ?? rows.length}
        aria-busy={isPlaceholder || undefined}
        className="scroll-region min-h-0 flex-1"
      >
        {/* 32px sticky header, one elevation step above the body. */}
        <div
          role="row"
          className={cn(
            'sticky top-0 z-10 grid h-8 items-center border-b border-default bg-elevated',
            'text-mini font-medium text-secondary',
          )}
          style={{ gridTemplateColumns: template }}
        >
          {selectable && <span role="columnheader" className="px-3" />}
          {columns.map((column) => {
            const sorted = sort?.columnId === column.id;
            return (
              <span
                key={column.id}
                role="columnheader"
                aria-sort={sorted ? (sort.direction === 'asc' ? 'ascending' : 'descending') : 'none'}
                className={cn(
                  'flex min-w-0 items-center gap-1 px-3',
                  column.align === 'right' && 'justify-end',
                )}
              >
                {column.sortable === true && onSortChange !== undefined ? (
                  <button
                    type="button"
                    onClick={() => {
                      onSortChange(column.id);
                    }}
                    className="inline-flex items-center gap-1 truncate rounded-xs hover:text-primary"
                  >
                    <span className="truncate">{column.header}</span>
                    {sorted &&
                      (sort.direction === 'asc' ? (
                        <ChevronUp className="size-3 text-accent-text" aria-hidden="true" />
                      ) : (
                        <ChevronDown className="size-3 text-accent-text" aria-hidden="true" />
                      ))}
                  </button>
                ) : (
                  <span className="truncate">{column.header}</span>
                )}
              </span>
            );
          })}
          {rowActions !== undefined && <span role="columnheader" className="px-3" />}
        </div>

        {rows.length === 0 ? (
          <div style={{ minHeight: rowHeight * Math.min(pageSize, 8) }}>{empty}</div>
        ) : virtualize ? (
          <div
            style={{
              height: virtualizer.getTotalSize(),
              minHeight: rowHeight * pageSize,
              position: 'relative',
            }}
          >
            {/* One transform for the whole window, not one per row. */}
            <div style={{ transform: `translateY(${String(offsetY)}px)` }}>
              {virtualItems.map((item) => {
                const row = rows[item.index];
                return row === undefined ? null : renderRow(row, item.index);
              })}
            </div>
          </div>
        ) : (
          <div style={{ minHeight: rowHeight * pageSize }}>
            {rows.map((row, index) => renderRow(row, index))}
          </div>
        )}
      </div>
    </div>
  );
}

/** Props for one rendered row. Internal — the table owns every one of them. */
interface TableRowProps<T extends { id: string }> {
  row: T;
  index: number;
  columns: readonly DataTableColumn<T>[];
  template: string;
  rowHeight: number;
  focused: boolean;
  selected: boolean;
  showCheckbox: boolean;
  selectable: boolean;
  onFocusRow?: (index: number) => void;
  onOpenRow?: (row: T, index: number) => void;
  onToggleSelect?: (row: T) => void;
  rowActions?: (row: T) => ReactNode;
  prefetchOptions?: (row: T) => PrefetchableOptions | null;
  style?: React.CSSProperties;
}

/**
 * One row.
 *
 * The checkbox cell shows the **row index in mono** until the row is hovered, focused, or
 * anything is selected — a column of forty permanently visible checkboxes is forty pieces of
 * chrome competing with the data (§7.5).
 */
function TableRow<T extends { id: string }>({
  row,
  index,
  columns,
  template,
  rowHeight,
  focused,
  selected,
  showCheckbox,
  selectable,
  onFocusRow,
  onOpenRow,
  onToggleSelect,
  rowActions,
  prefetchOptions,
  style,
}: TableRowProps<T>) {
  const prefetch = usePrefetch(prefetchOptions?.(row) ?? null);

  return (
    <div
      role="row"
      aria-rowindex={index + 1}
      aria-selected={selectable ? selected : undefined}
      data-row-index={index}
      tabIndex={focused ? 0 : -1}
      onMouseEnter={prefetch.onMouseEnter}
      onMouseLeave={prefetch.onMouseLeave}
      onBlur={prefetch.onBlur}
      // Focus does two things: it warms the row's detail query, and it is how a `Tab` into
      // the grid becomes the roving-tabindex anchor.
      onFocus={() => {
        prefetch.onFocus();
        onFocusRow?.(index);
      }}
      onClick={() => {
        onFocusRow?.(index);
        onOpenRow?.(row, index);
      }}
      className={cn(
        'group relative grid cursor-default items-center',
        'transition-colors duration-[140ms] ease-out-quad',
        'hover:bg-state-hover',
        // Inside the row box, so the ring never overlaps the neighbouring row.
        'focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-focus-ring',
        selected && 'bg-elevated',
      )}
      style={{ gridTemplateColumns: template, height: rowHeight, ...style }}
    >
      {/* Structural selection gets a 2px accent rail, full row height. */}
      {selected && (
        <span
          aria-hidden="true"
          className="absolute inset-y-0 left-0 w-0.5 bg-accent"
        />
      )}

      {selectable && (
        <span role="gridcell" className="flex items-center justify-center px-3">
          <span
            className={cn(
              'font-mono text-mini tabular-nums text-muted',
              (showCheckbox || focused || selected) && 'hidden',
              'group-hover:hidden group-focus-within:hidden',
            )}
          >
            {index + 1}
          </span>
          <span
            className={cn(
              'hidden',
              (showCheckbox || focused || selected) && 'block',
              'group-hover:block group-focus-within:block',
            )}
          >
            <Checkbox
              size="sm"
              checked={selected}
              aria-label={`Select row ${String(index + 1)}`}
              onCheckedChange={() => {
                onToggleSelect?.(row);
              }}
              onClick={(event) => {
                event.stopPropagation();
              }}
            />
          </span>
        </span>
      )}

      {columns.map((column) => (
        <span
          key={column.id}
          role="gridcell"
          className={cn(
            'flex min-w-0 items-center px-3',
            column.align === 'right' && 'justify-end',
            column.mono === true
              ? 'font-mono text-mini tabular-nums text-secondary'
              : 'text-sm text-primary',
          )}
        >
          <span className="truncate-1">{column.cell(row, index)}</span>
        </span>
      ))}

      {rowActions !== undefined && (
        <span
          role="gridcell"
          className="flex items-center justify-center px-3 opacity-0 transition-opacity duration-[140ms] ease-out-quad group-hover:opacity-100 group-focus-within:opacity-100"
          onClick={(event) => {
            event.stopPropagation();
          }}
        >
          {rowActions(row)}
        </span>
      )}
    </div>
  );
}
