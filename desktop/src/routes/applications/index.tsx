/**
 * The applications list (`docs/UI.md` §8.3).
 *
 * Two-line rows, because company and role both matter and truncating either one makes the
 * table unscannable. Virtualised past 100 rows with fixed heights and no `measureElement`
 * (§10.11), and never a spinner: the list query carries `keepPreviousData`, so changing a
 * filter dims the current rows for 120ms instead of emptying the table (§10.12).
 *
 * The row click opens the detail **Sheet** and `⌘↵` opens the full route (§9.4). The sheet is
 * the right surface because the list is still the thing being worked through: a decision about
 * one application is made against the shape of the others.
 */

import { Plus, RotateCw } from 'lucide-react';
import { useNavigate } from '@tanstack/react-router';
import { useCallback, useDeferredValue, useMemo, useState } from 'react';

import { ApplicationDetailPanel } from '@/components/application-detail';
import { FilterChip, Page, ResultCount, ToolbarSpacer } from '@/components/page';
import { prefetchable } from '@/components/prefetch';
import {
  Button,
  DataTable,
  EmptyState,
  NoResults,
  ScoreBar,
  SearchInput,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  Sheet,
  SheetContent,
  StatusBadge,
  type DataTableColumn,
} from '@/components/ui';
import {
  useApplication,
  useApplications,
  useRetryApplication,
} from '@/hooks';
import { usePagePrefetch } from '@/hooks/use-prefetch';
import { listNavigationHandlers, useShortcuts } from '@/hooks/use-shortcuts';
import {
  APPLICATION_STATUSES,
  ATS_PROVIDER_NAMES,
  PAGE_LIMIT,
  type ApplicationRead,
  type ApplicationStatus,
  type ATSProviderName,
} from '@/lib/api/types';
import { applicationDetailOptions, applicationListOptions } from '@/lib/query/options';
import { SHORTCUTS_BY_ID, comboLabel } from '@/lib/shortcuts';
import {
  formatRelative,
  orDash,
  providerLabel,
  shouldShowEmptyState,
  statusLabel,
} from '@/lib/utils';
import { activeFilterCount, useFiltersStore } from '@/stores/filters';
import { useRowHeight } from '@/stores/ui';

/** Two-line rows need 44px whatever the density setting says (§8.3). */
const MIN_ROW_HEIGHT = 44;

/**
 * The binding that opens the full route, spelled for this platform.
 *
 * Read from the keymap rather than written out, so the sheet says `Ctrl+Enter` on Windows and
 * `⌘↵` on macOS without this file knowing which one it is on.
 */
const OPEN_ROUTE_HINT = comboLabel(
  SHORTCUTS_BY_ID.get('list.openRoute')?.combo ?? { key: 'Enter', mod: true },
);

/** The applications table. */
export function ApplicationsRoute() {
  const navigate = useNavigate();
  const filters = useFiltersStore((state) => state.applications);
  const setFilters = useFiltersStore((state) => state.setApplications);
  const clearFilters = useFiltersStore((state) => state.clear);
  const density = useRowHeight();

  const [offset, setOffset] = useState(0);
  const [focusedIndex, setFocusedIndex] = useState(-1);
  const [openId, setOpenId] = useState<string | null>(null);

  const rawQuery = filters.q ?? '';
  const deferredQuery = useDeferredValue(rawQuery);

  const queryFilters = useMemo(
    () => ({ ...filters, q: deferredQuery, limit: PAGE_LIMIT, offset }),
    [filters, deferredQuery, offset],
  );

  const applications = useApplications(queryFilters);
  const rows = applications.data?.items ?? [];
  const total = applications.data?.total ?? 0;

  const makePageOptions = useCallback(
    (nextOffset: number) =>
      prefetchable(applicationListOptions({ ...queryFilters, offset: nextOffset })),
    [queryFilters],
  );
  usePagePrefetch(
    makePageOptions,
    offset,
    PAGE_LIMIT,
    rows.length,
    !applications.isPlaceholderData,
  );

  const openDetail = useApplication(openId ?? undefined);
  const retry = useRetryApplication();

  const focusedRow = focusedIndex >= 0 ? rows[focusedIndex] : undefined;

  useShortcuts({
    ...listNavigationHandlers(rows.length, focusedIndex, setFocusedIndex),
    'list.open': () => {
      if (focusedRow !== undefined) setOpenId(focusedRow.id);
    },
    'list.openRoute': () => {
      if (focusedRow !== undefined) {
        void navigate({ to: '/applications/$id', params: { id: focusedRow.id } });
      }
    },
    'row.retry': () => {
      if (focusedRow !== undefined && focusedRow.status === 'failed') {
        retry.mutate(focusedRow.id);
      }
    },
    'row.openExternal': () => {
      if (focusedRow?.posting != null) {
        window.open(focusedRow.posting.url, '_blank', 'noopener,noreferrer');
      }
    },
    'row.copyUrl': () => {
      if (focusedRow?.posting != null) {
        void navigator.clipboard.writeText(focusedRow.posting.url);
      }
    },
    'escape.pop': () => {
      if (openId !== null) setOpenId(null);
      else if (rawQuery !== '') setFilters({ q: '' });
      else setFocusedIndex(-1);
    },
  });

  const columns = useMemo<DataTableColumn<ApplicationRead>[]>(
    () => [
      {
        id: 'company',
        header: 'Company / Role',
        width: 'minmax(0, 2.4fr)',
        cell: (row) => (
          <span className="flex min-w-0 flex-col justify-center">
            <span className="truncate-1 text-sm text-primary">
              {orDash(row.company?.name ?? row.posting?.company?.name)}
            </span>
            <span className="truncate-1 text-mini text-muted">
              {orDash(row.posting?.title)}
            </span>
          </span>
        ),
      },
      {
        id: 'status',
        header: 'Status',
        width: '148px',
        cell: (row) => <StatusBadge status={row.status} />,
      },
      {
        id: 'score',
        header: 'Score',
        // 72px bar + gap + a three-character value + the cell's own padding. Narrower and
        // the number truncates, which is worse than no number at all.
        width: '140px',
        cell: (row) =>
          row.score == null ? <span className="text-muted">—</span> : (
            <ScoreBar value={row.score.normalized} />
          ),
      },
      {
        id: 'provider',
        header: 'Provider',
        width: '112px',
        mono: true,
        cell: (row) =>
          row.posting == null ? '—' : providerLabel(row.posting.provider),
      },
      {
        id: 'applied',
        header: 'Applied',
        width: '104px',
        align: 'right',
        mono: true,
        cell: (row) => formatRelative(row.submitted_at ?? row.created_at),
      },
    ],
    [],
  );

  const filterCount = activeFilterCount(filters);
  const empty = shouldShowEmptyState(rows.length === 0, applications.isPending, rawQuery);

  return (
    <Page
      title="Applications"
      subtitle={`${String(total)} total · ${String(rows.filter((row) => row.needs_review).length)} on this page need review`}
      busy={applications.isFetching}
      bleed
      actions={
        <Button
          variant="secondary"
          leadingIcon={<Plus aria-hidden="true" />}
          onClick={() => {
            void navigate({ to: '/postings' });
          }}
        >
          Apply from a posting
        </Button>
      }
      toolbar={
        <>
          <SearchInput
            value={rawQuery}
            placeholder="Search company or role…"
            aria-label="Search applications"
            data-page-search
            className="w-60 focus-within:w-80"
            onChange={(event) => {
              setOffset(0);
              setFilters({ q: event.target.value });
            }}
          />

          <Select
            value={filters.status ?? 'all'}
            onValueChange={(value) => {
              setOffset(0);
              setFilters({ status: value === 'all' ? undefined : (value as ApplicationStatus) });
            }}
          >
            <SelectTrigger size="sm" className="w-40" aria-label="Filter by status">
              <SelectValue placeholder="Status" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">Any status</SelectItem>
              {APPLICATION_STATUSES.map((status) => (
                <SelectItem key={status} value={status}>
                  {statusLabel(status)}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>

          <Select
            value={filters.provider ?? 'all'}
            onValueChange={(value) => {
              setOffset(0);
              setFilters({
                provider: value === 'all' ? undefined : (value as ATSProviderName),
              });
            }}
          >
            <SelectTrigger size="sm" className="w-36" aria-label="Filter by provider">
              <SelectValue placeholder="Provider" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">Any provider</SelectItem>
              {ATS_PROVIDER_NAMES.map((provider) => (
                <SelectItem key={provider} value={provider}>
                  {providerLabel(provider)}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>

          <FilterChip
            label="Needs review"
            active={filters.needs_review === true}
            onClick={() => {
              setOffset(0);
              setFilters({ needs_review: filters.needs_review === true ? undefined : true });
            }}
            onClear={() => {
              setFilters({ needs_review: undefined });
            }}
          />

          <ToolbarSpacer />
          <ResultCount count={rows.length} total={total} noun="rows" />
        </>
      }
    >
      <DataTable
        rows={rows}
        columns={columns}
        label="Applications"
        rowHeight={Math.max(density, MIN_ROW_HEIGHT)}
        pageSize={PAGE_LIMIT}
        totalCount={total}
        focusedIndex={focusedIndex}
        onFocusRow={setFocusedIndex}
        onOpenRow={(row) => {
          setOpenId(row.id);
        }}
        prefetchOptions={(row) => prefetchable(applicationDetailOptions(row.id))}
        isPlaceholder={applications.isPlaceholderData}
        className="h-[calc(100vh-260px)]"
        empty={
          !empty ? null : filterCount > 0 || rawQuery !== '' ? (
            <NoResults
              noun="applications"
              query={rawQuery}
              onClear={() => {
                setOffset(0);
                clearFilters('applications');
              }}
            />
          ) : (
            <EmptyState
              title="No applications yet"
              description="Applications appear the moment a run reaches a posting that clears your score threshold."
              primaryAction={{
                label: 'Browse postings',
                onClick: () => {
                  void navigate({ to: '/postings' });
                },
              }}
            />
          )
        }
        rowActions={(row) =>
          row.status === 'failed' ? (
            <Button
              variant="ghost"
              size="sm"
              icon
              aria-label="Retry this application"
              onClick={() => {
                retry.mutate(row.id);
              }}
            >
              <RotateCw aria-hidden="true" />
            </Button>
          ) : null
        }
      />

      {total > PAGE_LIMIT && (
        <div className="mt-3 flex items-center justify-end gap-2">
          <span className="font-mono text-mini tabular-nums text-muted">
            {offset + 1}–{offset + rows.length} of {total}
          </span>
          <Button
            variant="secondary"
            size="sm"
            disabled={offset === 0}
            onClick={() => {
              setOffset((current) => Math.max(0, current - PAGE_LIMIT));
            }}
          >
            Previous
          </Button>
          <Button
            variant="secondary"
            size="sm"
            disabled={offset + rows.length >= total}
            onClick={() => {
              setOffset((current) => current + PAGE_LIMIT);
            }}
          >
            Next
          </Button>
        </div>
      )}

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
          title="Application"
          description={`Press ${OPEN_ROUTE_HINT} to open the full record.`}
          footer={
            openId === null ? undefined : (
              <Button
                variant="secondary"
                onClick={() => {
                  const id = openId;
                  setOpenId(null);
                  void navigate({ to: '/applications/$id', params: { id } });
                }}
              >
                Open the full record
              </Button>
            )
          }
        >
          {openId !== null && (
            <ApplicationDetailPanel
              applicationId={openId}
              summary={rows.find((row) => row.id === openId)}
              detail={openDetail.data}
              isPending={openDetail.isPending}
              retrying={retry.isPending}
              onRetry={() => {
                retry.mutate(openId);
              }}
            />
          )}
        </SheetContent>
      </Sheet>
    </Page>
  );
}
