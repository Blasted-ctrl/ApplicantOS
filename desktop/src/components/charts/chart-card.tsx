/**
 * The frame every chart in the product sits in (`docs/UI.md` §11.3, §11.4, §12.4).
 *
 * Three things it guarantees so no individual chart has to remember them:
 *
 * **A table view exists for every chart, always.** `⌥T` while the card has focus, or the
 * toggle in its header. §11.4 calls it the accessibility relief channel and says it is not
 * optional — and it is doubly load-bearing in light theme, where three of the eight validated
 * series slots fall below 3:1 on white and the relief rule applies.
 *
 * **A legend appears for two or more series and never for one.** A single-series chart is
 * already named by its title; a legend there is a key to a lock with one key.
 *
 * **An empty chart is an `EmptyState`, never an empty axis frame.** A pair of axes with no
 * marks reads as a rendering failure rather than as "nothing has happened yet".
 *
 * The card carries `role="img"` and an `aria-label` that states the *takeaway*, not the
 * encoding: "82 applications over 30 days, peaking at 9 on August 4" is useful; "bar chart of
 * applications" is not.
 */

import { BarChart3, Table2 } from 'lucide-react';
import { useCallback, useState, type KeyboardEvent, type ReactNode } from 'react';

import { Button, Card, CardBody, CardHeader, EmptyState, Tooltip } from '@/components/ui';
import { cn } from '@/lib/utils';

/** One legend entry. Colour is a `var()` reference so it follows the theme. */
export interface ChartSeries {
  key: string;
  label: string;
  color: string;
}

/** The mandatory table rendering of the same data. */
export interface ChartTable {
  columns: readonly string[];
  rows: readonly (readonly (string | number)[])[];
}

/** Props for {@link ChartCard}. */
export interface ChartCardProps {
  title: string;
  /** One line under the title: the window, the sample size, the caveat. */
  subtitle?: ReactNode;
  /** The takeaway, for screen readers and for the `title` attribute. */
  summary: string;
  /** Two or more series produce a legend; one or zero produce none. */
  series?: readonly ChartSeries[];
  /** The same data as a real `<table>`. Required — §11.4 has no exemption. */
  table: ChartTable;
  /** True when there is genuinely nothing to plot. */
  isEmpty?: boolean;
  /** Copy for the empty state. */
  emptyTitle?: string;
  emptyDescription?: ReactNode;
  /** 200px inside a card, 280px full-width (§11.3). */
  height?: number;
  className?: string;
  children: ReactNode;
}

/** A titled chart surface with its legend, its table view and its empty state. */
export function ChartCard({
  title,
  subtitle,
  summary,
  series,
  table,
  isEmpty = false,
  emptyTitle = 'Nothing to chart yet',
  emptyDescription,
  height = 200,
  className,
  children,
}: ChartCardProps) {
  const [showTable, setShowTable] = useState(false);

  const onKeyDown = useCallback((event: KeyboardEvent<HTMLDivElement>) => {
    if (event.altKey && event.code === 'KeyT') {
      event.preventDefault();
      setShowTable((current) => !current);
    }
  }, []);

  const legend = series !== undefined && series.length >= 2;

  return (
    <Card
      tabIndex={0}
      onKeyDown={onKeyDown}
      className={cn('flex min-w-0 flex-col', className)}
      aria-label={`${title}. ${summary}`}
    >
      <CardHeader
        title={title}
        {...(subtitle === undefined ? {} : { subtitle })}
        actions={
          <Tooltip content={showTable ? 'Show the chart' : 'Show the data table'} shortcut="⌥T">
            <Button
              variant="ghost"
              size="sm"
              icon
              aria-pressed={showTable}
              aria-label={showTable ? 'Show the chart' : 'Show the data table'}
              onClick={() => {
                setShowTable((current) => !current);
              }}
            >
              {showTable ? <BarChart3 aria-hidden="true" /> : <Table2 aria-hidden="true" />}
            </Button>
          </Tooltip>
        }
      />

      {legend && (
        <div className="flex flex-wrap items-center justify-end gap-3 px-4 pb-2">
          {series.map((entry) => (
            <span key={entry.key} className="inline-flex items-center gap-1.5 text-mini text-secondary">
              <span
                aria-hidden="true"
                className="size-2 rounded-full"
                style={{ backgroundColor: entry.color }}
              />
              {entry.label}
            </span>
          ))}
        </div>
      )}

      <CardBody className="min-w-0 flex-1">
        {isEmpty ? (
          <div style={{ minHeight: height }} className="flex items-center justify-center">
            <EmptyState
              icon={BarChart3}
              title={emptyTitle}
              {...(emptyDescription === undefined ? {} : { description: emptyDescription })}
            />
          </div>
        ) : showTable ? (
          <ChartTableView table={table} height={height} />
        ) : (
          <div role="img" aria-label={`${title}. ${summary}`} style={{ height }}>
            {children}
          </div>
        )}
      </CardBody>
    </Card>
  );
}

/** The chart's data as a real table — the accessible alternative, not a fallback. */
function ChartTableView({ table, height }: { table: ChartTable; height: number }) {
  return (
    <div className="scroll-region" style={{ maxHeight: height }}>
      <table className="w-full border-collapse text-sm">
        <thead>
          <tr>
            {table.columns.map((column, index) => (
              <th
                key={column}
                scope="col"
                className={cn(
                  'sticky top-0 z-10 bg-surface pb-1 text-mini font-medium text-muted',
                  index === 0 ? 'text-left' : 'text-right',
                )}
              >
                {column}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {table.rows.map((row) => (
            <tr key={String(row[0])} className="border-t border-state-divider">
              {row.map((cell, index) => (
                <td
                  key={`${String(row[0])}-${String(index)}`}
                  className={cn(
                    'py-1',
                    index === 0
                      ? 'text-left text-secondary'
                      : 'text-right font-mono text-mini tabular-nums text-primary',
                  )}
                >
                  {cell}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
