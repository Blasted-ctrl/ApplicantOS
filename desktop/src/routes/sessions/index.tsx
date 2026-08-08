/**
 * Runs (`docs/UI.md` §8.9).
 *
 * The executing run is a pinned card above the history table, and **every counter on it is
 * mono, `tabular-nums`, and updates with no animation at all** (§P5). A number that counts up
 * is a number the reader has to wait for; at one update a second across seven counters, the
 * animation would be the only thing on screen that ever moves.
 *
 * A history row opens a Sheet with the run's `config_snapshot` and `token_usage` — the two
 * things that answer "why did that run behave differently from this one" — plus the
 * applications it produced.
 */

import { Play, Square } from 'lucide-react';
import { useNavigate } from '@tanstack/react-router';
import { useMemo, useState } from 'react';

import { useAppActions } from '@/components/app-actions';
import { Page, ResultCount, ToolbarSpacer } from '@/components/page';
import {
  Badge,
  Button,
  Card,
  CardBody,
  CardHeader,
  DataTable,
  EmptyState,
  Kbd,
  ProgressRing,
  Sheet,
  SheetContent,
  StatusDot,
  type DataTableColumn,
} from '@/components/ui';
import { useSession, useSessions } from '@/hooks';
import { useActiveSession } from '@/hooks/use-sessions';
import { PAGE_LIMIT, type SessionRead } from '@/lib/api/types';
import {
  cn,
  formatDateTime,
  formatDuration,
  formatNumber,
  formatRelative,
  orDash,
  sessionStatusTone,
  statusLabel,
} from '@/lib/utils';
import { useRowHeight } from '@/stores/ui';

/** Average of the finite values in a list, or `null`. */
function average(values: readonly number[]): number | null {
  const usable = values.filter((value) => Number.isFinite(value));
  if (usable.length === 0) return null;
  return usable.reduce((total, value) => total + value, 0) / usable.length;
}

/** Sum the numeric values of a token-usage map. */
function totalTokens(usage: Readonly<Record<string, unknown>>): number {
  return Object.values(usage).reduce<number>(
    (total, value) => total + (typeof value === 'number' ? value : 0),
    0,
  );
}

/** Run history. */
export function SessionsRoute() {
  const navigate = useNavigate();
  const { startRun, stopRun } = useAppActions();
  const density = useRowHeight();
  const [openId, setOpenId] = useState<string | null>(null);
  const [focusedIndex, setFocusedIndex] = useState(-1);

  const sessions = useSessions({ limit: PAGE_LIMIT });
  const activeSession = useActiveSession();
  const detail = useSession(openId ?? undefined);

  const rows = useMemo(() => sessions.data?.items ?? [], [sessions.data]);
  const total = sessions.data?.total ?? 0;
  const finished = rows.filter((session) => session.status !== 'running');

  const averageDuration = average(
    finished.map((session) => session.duration_seconds ?? Number.NaN),
  );
  const averagePerApplication = average(
    finished.map((session) => session.avg_application_seconds ?? Number.NaN),
  );

  const columns = useMemo<DataTableColumn<SessionRead>[]>(
    () => [
      {
        id: 'started',
        header: 'Started',
        width: 'minmax(0, 1.4fr)',
        mono: true,
        cell: (row) => formatDateTime(row.started_at),
      },
      {
        id: 'status',
        header: 'Status',
        width: '124px',
        cell: (row) => {
          const tone = sessionStatusTone(row.status);
          return <Badge tone={tone}>{tone.label}</Badge>;
        },
      },
      {
        id: 'found',
        header: 'Found',
        width: '84px',
        align: 'right',
        mono: true,
        cell: (row) => formatNumber(row.jobs_found),
      },
      {
        id: 'qualified',
        header: 'Qualified',
        width: '92px',
        align: 'right',
        mono: true,
        cell: (row) => formatNumber(row.jobs_qualified),
      },
      {
        id: 'applied',
        header: 'Applied',
        width: '86px',
        align: 'right',
        mono: true,
        cell: (row) => formatNumber(row.applications_completed),
      },
      {
        id: 'review',
        header: 'Review',
        width: '84px',
        align: 'right',
        mono: true,
        cell: (row) => (
          <span className={cn(row.manual_review > 0 && 'text-st-review')}>
            {formatNumber(row.manual_review)}
          </span>
        ),
      },
      {
        id: 'failed',
        header: 'Failed',
        width: '80px',
        align: 'right',
        mono: true,
        cell: (row) => (
          <span className={cn(row.failures > 0 && 'text-st-danger')}>
            {formatNumber(row.failures)}
          </span>
        ),
      },
      {
        id: 'duration',
        header: 'Duration',
        width: '96px',
        align: 'right',
        mono: true,
        cell: (row) => formatDuration(row.duration_seconds),
      },
    ],
    [],
  );

  return (
    <Page
      title="Runs"
      subtitle={`${activeSession === null ? '0 running' : '1 running'} · ${String(total)} total${averageDuration === null ? '' : ` · avg ${formatDuration(averageDuration)}`}${averagePerApplication === null ? '' : ` · avg ${formatDuration(averagePerApplication)} per application`}`}
      busy={sessions.isFetching}
      bleed
      actions={
        <>
          <Button
            variant="secondary"
            disabled={activeSession === null}
            leadingIcon={<Square aria-hidden="true" />}
            trailingIcon={<Kbd keys={['Ctrl', 'S']} />}
            onClick={stopRun}
          >
            Stop
          </Button>
          <Button
            variant="primary"
            leadingIcon={<Play aria-hidden="true" />}
            trailingIcon={<Kbd keys={['Ctrl', 'Shift', 'R']} />}
            onClick={startRun}
          >
            Start a run
          </Button>
        </>
      }
      toolbar={
        <>
          <ToolbarSpacer />
          <ResultCount count={rows.length} total={total} noun="runs" />
        </>
      }
    >
      {activeSession !== null && (
        <Card className="mb-3 shadow-selected">
          <CardHeader
            title={
              <span className="inline-flex items-center gap-2">
                <StatusDot status="submitting" aria-hidden="true" />
                Running
              </span>
            }
            subtitle={`started ${formatRelative(activeSession.started_at)} · ${formatDuration((Date.now() - new Date(activeSession.started_at).getTime()) / 1000)} elapsed · trigger ${activeSession.trigger}`}
          />
          <CardBody className="flex flex-wrap items-center gap-6">
            <ProgressRing
              size="lg"
              showLabel
              label="Applications completed out of qualified postings"
              value={
                activeSession.jobs_qualified === 0
                  ? 0
                  : activeSession.applications_completed / activeSession.jobs_qualified
              }
            />
            <Counter label="Found" value={activeSession.jobs_found} />
            <Counter label="Qualified" value={activeSession.jobs_qualified} />
            <Counter label="Résumés" value={activeSession.resumes_generated} />
            <Counter label="Applied" value={activeSession.applications_completed} />
            <Counter label="Review" value={activeSession.manual_review} tone="review" />
            <Counter label="Failed" value={activeSession.failures} tone="danger" />
            <Counter label="Tokens" value={totalTokens(activeSession.token_usage)} />
          </CardBody>
        </Card>
      )}

      <DataTable
        rows={rows}
        columns={columns}
        label="Run history"
        rowHeight={density}
        pageSize={20}
        totalCount={total}
        focusedIndex={focusedIndex}
        onFocusRow={setFocusedIndex}
        onOpenRow={(row) => {
          setOpenId(row.id);
        }}
        empty={
          sessions.isPending ? null : (
            <EmptyState
              icon={Play}
              title="No runs yet"
              description="A run discovers postings, scores them, tailors documents and — if both safety switches allow it — submits."
              primaryAction={{ label: 'Start a run', onClick: startRun }}
            />
          )
        }
      />

      <Sheet
        open={openId !== null}
        onOpenChange={(open) => {
          if (!open) setOpenId(null);
        }}
      >
        <SheetContent
          open={openId !== null}
          onOpenChange={(open) => {
            if (!open) setOpenId(null);
          }}
          size="lg"
          title="Run detail"
          description={
            detail.data === undefined
              ? undefined
              : `${formatDateTime(detail.data.started_at)} · ${sessionStatusTone(detail.data.status).label}`
          }
        >
          {detail.data === undefined ? null : (
            <div className="flex flex-col gap-4">
              <section>
                <h3 className="label-caps mb-2">Counters</h3>
                <div className="grid grid-cols-3 gap-3">
                  <Counter label="Found" value={detail.data.jobs_found} />
                  <Counter label="Qualified" value={detail.data.jobs_qualified} />
                  <Counter label="Résumés" value={detail.data.resumes_generated} />
                  <Counter label="Applied" value={detail.data.applications_completed} />
                  <Counter label="Review" value={detail.data.manual_review} tone="review" />
                  <Counter label="Failed" value={detail.data.failures} tone="danger" />
                  <Counter label="Checkpoints pending" value={detail.data.checkpoints_pending} />
                  <Counter label="Tokens" value={totalTokens(detail.data.token_usage)} />
                </div>
              </section>

              <section>
                <h3 className="label-caps mb-2">Applications produced</h3>
                {detail.data.applications.length === 0 ? (
                  <p className="text-sm text-muted">This run produced no applications.</p>
                ) : (
                  <ul className="flex flex-col">
                    {detail.data.applications.map((application) => (
                      <li key={application.id}>
                        <button
                          type="button"
                          onClick={() => {
                            setOpenId(null);
                            void navigate({
                              to: '/applications/$id',
                              params: { id: application.id },
                            });
                          }}
                          className="flex w-full items-center gap-3 border-b border-state-divider py-1.5 text-left last:border-b-0 hover:bg-state-hover"
                        >
                          <StatusDot status={application.status} aria-hidden="true" />
                          <span className="min-w-0 flex-1 truncate text-sm text-primary">
                            {orDash(application.company?.name)} ·{' '}
                            {orDash(application.posting?.title)}
                          </span>
                          <span className="shrink-0 text-mini text-muted">
                            {statusLabel(application.status)}
                          </span>
                        </button>
                      </li>
                    ))}
                  </ul>
                )}
              </section>

              <section>
                <h3 className="label-caps mb-2">Configuration snapshot</h3>
                <pre className="scroll-region max-h-64 rounded-md border border-default bg-inset p-3 font-mono text-micro tracking-normal text-secondary">
                  {JSON.stringify(detail.data.config_snapshot, null, 2)}
                </pre>
              </section>
            </div>
          )}
        </SheetContent>
      </Sheet>
    </Page>
  );
}

/** One run counter. Mono, tabular, and never animated. */
function Counter({
  label,
  value,
  tone,
}: {
  label: string;
  value: number;
  tone?: 'review' | 'danger';
}) {
  return (
    <div className="flex flex-col">
      <span className="text-mini text-muted">{label}</span>
      <span
        className={cn(
          'font-mono text-lg tabular-nums',
          tone === 'review' && value > 0 && 'text-st-review',
          tone === 'danger' && value > 0 && 'text-st-danger',
          (tone === undefined || value === 0) && 'text-primary',
        )}
      >
        {formatNumber(value)}
      </span>
    </div>
  );
}
