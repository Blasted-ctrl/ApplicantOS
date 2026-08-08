/**
 * One application, in the master–detail layout (`docs/UI.md` §8.4).
 *
 * The list stays on the left at `minmax(420px, 1fr)` and the detail sits at
 * `minmax(480px, 620px)`. Switching rows never remounts the detail shell — the panel is one
 * component reading a different id — which is what makes `j`/`k` down the list feel like
 * scrolling rather than like twenty navigations.
 *
 * **The header paints at frame one.** The list page is already in cache, so the row for this
 * id is handed to the panel as its summary while the full payload is still in flight. That is
 * the §8.4 instant-open rule, expressed as a prop rather than as a partial write into the
 * detail cache key.
 */

import { ArrowLeft } from 'lucide-react';
import { useNavigate } from '@tanstack/react-router';
import { useMemo } from 'react';

import { ApplicationDetailPanel } from '@/components/application-detail';
import { Page } from '@/components/page';
import { Button, EmptyState, ScoreBar, StatusDot } from '@/components/ui';
import { useApplication, useApplications, useRetryApplication } from '@/hooks';
import { useShortcuts } from '@/hooks/use-shortcuts';
import { PAGE_LIMIT } from '@/lib/api/types';
import { cn, formatRelative, orDash } from '@/lib/utils';
import { useFiltersStore } from '@/stores/filters';
import { useUiStore } from '@/stores/ui';

/** Props for {@link ApplicationDetailRoute}. */
export interface ApplicationDetailRouteProps {
  id: string;
}

/** The full record for one application, with its siblings alongside. */
export function ApplicationDetailRoute({ id }: ApplicationDetailRouteProps) {
  const navigate = useNavigate();
  const filters = useFiltersStore((state) => state.applications);
  const listOpen = useUiStore((state) => state.detailPaneOpen);
  const toggleList = useUiStore((state) => state.toggleDetailPane);

  const applications = useApplications({ ...filters, limit: PAGE_LIMIT, offset: 0 });
  const detail = useApplication(id);
  const retry = useRetryApplication();

  const rows = useMemo(() => applications.data?.items ?? [], [applications.data]);
  const summary = useMemo(() => rows.find((row) => row.id === id), [rows, id]);
  const index = rows.findIndex((row) => row.id === id);

  const move = (delta: number) => {
    const next = rows[index + delta];
    if (next !== undefined) {
      void navigate({ to: '/applications/$id', params: { id: next.id } });
    }
  };

  useShortcuts({
    'list.next': () => {
      move(1);
    },
    'list.prev': () => {
      move(-1);
    },
    'detail.toggle': toggleList,
    'row.retry': () => {
      if ((detail.data ?? summary)?.status === 'failed') retry.mutate(id);
    },
    'escape.pop': () => {
      void navigate({ to: '/applications' });
    },
  });

  const head = detail.data ?? summary;
  const title =
    head === undefined
      ? 'Application'
      : `${orDash(head.company?.name ?? head.posting?.company?.name)}${head.posting?.title === undefined ? '' : ` · ${head.posting.title}`}`;

  return (
    <Page
      title={title}
      subtitle={
        head === undefined
          ? 'Loading this record from the local cache.'
          : `${String(head.attempt_count)} ${head.attempt_count === 1 ? 'attempt' : 'attempts'} · updated ${formatRelative(head.updated_at)}`
      }
      busy={detail.isFetching}
      bleed
      actions={
        <Button
          variant="ghost"
          leadingIcon={<ArrowLeft aria-hidden="true" />}
          onClick={() => {
            void navigate({ to: '/applications' });
          }}
        >
          All applications
        </Button>
      }
    >
      <div
        className="grid h-full min-h-0 gap-4"
        style={{
          gridTemplateColumns: listOpen
            ? 'minmax(320px, 1fr) minmax(480px, 620px)'
            : 'minmax(0, 1fr)',
        }}
      >
        {listOpen && (
          <aside className="scroll-region min-h-0 rounded-lg border border-default bg-surface">
            {rows.length === 0 ? (
              <EmptyState
                title="The list is empty"
                description="This record was opened directly. Clear your filters to see its siblings."
              />
            ) : (
              <ul>
                {rows.map((row) => (
                  <li key={row.id}>
                    <button
                      type="button"
                      onClick={() => {
                        void navigate({ to: '/applications/$id', params: { id: row.id } });
                      }}
                      className={cn(
                        'flex w-full items-center gap-3 px-3 py-2 text-left',
                        'transition-colors duration-[140ms] ease-out-quad hover:bg-state-hover',
                        row.id === id && 'bg-accent-subtle',
                      )}
                    >
                      <StatusDot status={row.status} aria-hidden="true" />
                      <span className="min-w-0 flex-1">
                        <span className="block truncate text-sm text-primary">
                          {orDash(row.company?.name ?? row.posting?.company?.name)}
                        </span>
                        <span className="block truncate text-mini text-muted">
                          {orDash(row.posting?.title)}
                        </span>
                      </span>
                      {row.score != null && (
                        <ScoreBar value={row.score.normalized} className="shrink-0" />
                      )}
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </aside>
        )}

        <section className="scroll-region min-h-0 rounded-lg border border-default bg-surface p-4">
          <ApplicationDetailPanel
            applicationId={id}
            summary={summary}
            detail={detail.data}
            isPending={detail.isPending}
            retrying={retry.isPending}
            onRetry={() => {
              retry.mutate(id);
            }}
          />
        </section>
      </div>
    </Page>
  );
}
