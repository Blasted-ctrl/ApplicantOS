/**
 * The postings browser (`docs/UI.md` §8.5).
 *
 * The same table shell as Applications with different columns and a discovery action. Two
 * details carry more weight than they look like they should:
 *
 * **Policy flags are rendered under the company line.** `is_defense`, `is_startup` and a
 * blocked company or industry are why a posting scored low, and showing them inline means the
 * score explains itself without a click. A low number with no visible cause reads as a bug in
 * the scorer.
 *
 * **`Apply now` only exists where it can work.** LinkedIn's terms prohibit automated
 * submission and Workday's account-gated flow routes to review by design, so the action is
 * gated on `AUTO_APPLY_PROVIDERS` (golden rule #10). Offering a button the backend will refuse
 * would be a promise this client is not in a position to make.
 */

import { Search, Send } from 'lucide-react';
import { useCallback, useDeferredValue, useMemo, useState } from 'react';

import { useAppActions } from '@/components/app-actions';
import { FilterChip, Page, ResultCount, ToolbarSpacer } from '@/components/page';
import { prefetchable } from '@/components/prefetch';
import { ScoreBreakdown } from '@/components/score-breakdown';
import {
  Badge,
  Button,
  DataTable,
  EmptyState,
  Kbd,
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
  Tooltip,
  type DataTableColumn,
} from '@/components/ui';
import { canAutoApply, useApplyToPosting, usePosting, usePostings, usePreferences } from '@/hooks';
import { usePagePrefetch } from '@/hooks/use-prefetch';
import { listNavigationHandlers, useShortcuts } from '@/hooks/use-shortcuts';
import {
  ATS_PROVIDER_NAMES,
  PAGE_LIMIT,
  POSTING_STATUSES,
  type ATSProviderName,
  type PostingRead,
  type PostingStatus,
} from '@/lib/api/types';
import { postingDetailOptions, postingListOptions } from '@/lib/query/options';
import {
  EM_DASH,
  formatRelative,
  humanize,
  orDash,
  postingStatusTone,
  providerLabel,
  shouldShowEmptyState,
} from '@/lib/utils';
import { activeFilterCount, useFiltersStore } from '@/stores/filters';
import { useRowHeight } from '@/stores/ui';

/** Two-line rows need 44px whatever the density setting says. */
const MIN_ROW_HEIGHT = 44;

/** Score at or above which the "high score" chip filters. */
const HIGH_SCORE = 70;

/** Days behind which the "recent" chip filters. */
const RECENT_DAYS = 7;

/** `$180–220K`, `$180K+`, or an em-dash. Never a fabricated midpoint. */
function formatSalary(posting: PostingRead): string {
  const { salary_min: low, salary_max: high, salary_currency: currency } = posting;
  const symbol = currency === 'USD' || currency == null ? '$' : `${currency} `;
  const compact = (value: number): string =>
    value >= 1000 ? `${String(Math.round(value / 1000))}K` : String(value);
  if (low == null && high == null) return EM_DASH;
  if (low != null && high != null) return `${symbol}${compact(low)}–${compact(high)}`;
  if (low != null) return `${symbol}${compact(low)}+`;
  return `up to ${symbol}${compact(high ?? 0)}`;
}

/** Policy flags worth surfacing under the company line. */
function policyFlags(posting: PostingRead): string[] {
  const flags: string[] = [];
  if (posting.company?.is_defense === true) flags.push('defense');
  if (posting.company?.is_startup === true) flags.push('startup');
  return flags;
}

/** The postings table. */
export function PostingsRoute() {
  const { discover } = useAppActions();
  const filters = useFiltersStore((state) => state.postings);
  const setFilters = useFiltersStore((state) => state.setPostings);
  const clearFilters = useFiltersStore((state) => state.clear);
  const density = useRowHeight();
  const { data: preferences } = usePreferences();

  const [offset, setOffset] = useState(0);
  const [focusedIndex, setFocusedIndex] = useState(-1);
  const [openId, setOpenId] = useState<string | null>(null);

  const rawQuery = filters.q ?? '';
  const deferredQuery = useDeferredValue(rawQuery);

  const queryFilters = useMemo(
    () => ({ ...filters, q: deferredQuery, limit: PAGE_LIMIT, offset }),
    [filters, deferredQuery, offset],
  );

  const postings = usePostings(queryFilters);
  const rows = postings.data?.items ?? [];
  const total = postings.data?.total ?? 0;
  const openPosting = usePosting(openId ?? undefined);
  const apply = useApplyToPosting();

  const makePageOptions = useCallback(
    (nextOffset: number) =>
      prefetchable(postingListOptions({ ...queryFilters, offset: nextOffset })),
    [queryFilters],
  );
  usePagePrefetch(makePageOptions, offset, PAGE_LIMIT, rows.length, !postings.isPlaceholderData);

  const focusedRow = focusedIndex >= 0 ? rows[focusedIndex] : undefined;

  useShortcuts({
    ...listNavigationHandlers(rows.length, focusedIndex, setFocusedIndex),
    'list.open': () => {
      if (focusedRow !== undefined) setOpenId(focusedRow.id);
    },
    'row.apply': () => {
      if (focusedRow !== undefined && canAutoApply(focusedRow)) apply.mutate(focusedRow.id);
    },
    'row.copyUrl': () => {
      if (focusedRow !== undefined) void navigator.clipboard.writeText(focusedRow.url);
    },
    'row.openExternal': () => {
      if (focusedRow !== undefined) {
        window.open(focusedRow.url, '_blank', 'noopener,noreferrer');
      }
    },
    'escape.pop': () => {
      if (openId !== null) setOpenId(null);
      else if (rawQuery !== '') setFilters({ q: '' });
      else setFocusedIndex(-1);
    },
  });

  const columns = useMemo<DataTableColumn<PostingRead>[]>(
    () => [
      {
        id: 'title',
        header: 'Title / Company',
        width: 'minmax(0, 2.4fr)',
        cell: (row) => {
          const flags = policyFlags(row);
          return (
            <span className="flex min-w-0 flex-col justify-center">
              <span className="truncate-1 text-sm text-primary">{row.title}</span>
              <span className="truncate-1 flex items-center gap-1.5 text-mini text-muted">
                {orDash(row.company?.name)} · {providerLabel(row.provider)}
                {flags.map((flag) => (
                  <span key={flag} className="text-micro tracking-normal text-st-review">
                    {flag}
                  </span>
                ))}
              </span>
            </span>
          );
        },
      },
      {
        id: 'score',
        header: 'Score',
        // 72px bar + gap + a three-character value + the cell's own padding.
        width: '140px',
        cell: (row) =>
          row.score == null ? (
            <span className="text-muted">{EM_DASH}</span>
          ) : (
            <ScoreBar
              value={row.score.normalized}
              {...(preferences === undefined ? {} : { threshold: preferences.min_score })}
            />
          ),
      },
      {
        id: 'salary',
        header: 'Salary',
        width: '128px',
        mono: true,
        cell: (row) => formatSalary(row),
      },
      {
        id: 'arrangement',
        header: 'Arrangement',
        width: '112px',
        cell: (row) => humanize(row.work_arrangement),
      },
      {
        id: 'posted',
        header: 'Posted',
        width: '96px',
        align: 'right',
        mono: true,
        cell: (row) => formatRelative(row.posted_at ?? row.created_at),
      },
      {
        id: 'status',
        header: 'Status',
        width: '112px',
        cell: (row) => {
          const tone = postingStatusTone(row.status);
          return <Badge tone={tone}>{tone.label}</Badge>;
        },
      },
    ],
    [preferences],
  );

  const filterCount = activeFilterCount(filters);
  const empty = shouldShowEmptyState(rows.length === 0, postings.isPending, rawQuery);

  return (
    <Page
      title="Postings"
      subtitle={`${String(total)} discovered · ${String(rows.filter((row) => (row.score?.normalized ?? 0) >= (preferences?.min_score ?? HIGH_SCORE)).length)} on this page clear your threshold`}
      busy={postings.isFetching}
      bleed
      actions={
        <Button
          variant="primary"
          leadingIcon={<Search aria-hidden="true" />}
          trailingIcon={<Kbd keys={['Ctrl', 'Shift', 'D']} />}
          onClick={discover}
        >
          Discover
        </Button>
      }
      toolbar={
        <>
          <SearchInput
            value={rawQuery}
            placeholder="Search title or company…"
            aria-label="Search postings"
            data-page-search
            className="w-60 focus-within:w-80"
            onChange={(event) => {
              setOffset(0);
              setFilters({ q: event.target.value });
            }}
          />

          <Select
            value={filters.provider ?? 'all'}
            onValueChange={(value) => {
              setOffset(0);
              setFilters({ provider: value === 'all' ? undefined : (value as ATSProviderName) });
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

          <Select
            value={filters.status ?? 'all'}
            onValueChange={(value) => {
              setOffset(0);
              setFilters({ status: value === 'all' ? undefined : (value as PostingStatus) });
            }}
          >
            <SelectTrigger size="sm" className="w-36" aria-label="Filter by status">
              <SelectValue placeholder="Status" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">Any status</SelectItem>
              {POSTING_STATUSES.map((status) => (
                <SelectItem key={status} value={status}>
                  {postingStatusTone(status).label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>

          <FilterChip
            label={`Score ≥ ${String(preferences?.min_score ?? HIGH_SCORE)}`}
            active={filters.min_score !== undefined}
            onClick={() => {
              setOffset(0);
              setFilters({
                min_score:
                  filters.min_score === undefined ? (preferences?.min_score ?? HIGH_SCORE) : undefined,
              });
            }}
            onClear={() => {
              setFilters({ min_score: undefined });
            }}
          />

          <FilterChip
            label="Remote"
            active={filters.remote_only === true}
            onClick={() => {
              setOffset(0);
              setFilters({ remote_only: filters.remote_only === true ? undefined : true });
            }}
            onClear={() => {
              setFilters({ remote_only: undefined });
            }}
          />

          <FilterChip
            label={`Posted ≤ ${String(RECENT_DAYS)}d`}
            active={filters.since !== undefined}
            onClick={() => {
              setOffset(0);
              setFilters({
                since:
                  filters.since === undefined
                    ? new Date(Date.now() - RECENT_DAYS * 86_400_000).toISOString()
                    : undefined,
              });
            }}
            onClear={() => {
              setFilters({ since: undefined });
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
        label="Postings"
        rowHeight={Math.max(density, MIN_ROW_HEIGHT)}
        pageSize={PAGE_LIMIT}
        totalCount={total}
        focusedIndex={focusedIndex}
        onFocusRow={setFocusedIndex}
        onOpenRow={(row) => {
          setOpenId(row.id);
        }}
        prefetchOptions={(row) => prefetchable(postingDetailOptions(row.id))}
        isPlaceholder={postings.isPlaceholderData}
        className="h-[calc(100vh-260px)]"
        empty={
          !empty ? null : filterCount > 0 || rawQuery !== '' ? (
            <NoResults
              noun="postings"
              query={rawQuery}
              onClear={() => {
                setOffset(0);
                clearFilters('postings');
              }}
            />
          ) : (
            <EmptyState
              icon={Search}
              title="No postings discovered yet"
              description="Discovery reads the ATS providers you have enabled. Nothing is scraped that a provider does not publish."
              primaryAction={{ label: 'Discover postings', onClick: discover }}
            />
          )
        }
        rowActions={(row) =>
          canAutoApply(row) ? (
            <Tooltip content="Queue this posting for the apply pipeline">
              <Button
                variant="ghost"
                size="sm"
                icon
                aria-label={`Apply to ${row.title}`}
                onClick={() => {
                  apply.mutate(row.id);
                }}
              >
                <Send aria-hidden="true" />
              </Button>
            </Tooltip>
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
          title={openPosting.data?.title ?? 'Posting'}
          description={
            openPosting.data === undefined
              ? undefined
              : `${orDash(openPosting.data.company?.name)} · ${providerLabel(openPosting.data.provider)}`
          }
          footer={
            openPosting.data !== undefined && canAutoApply(openPosting.data) ? (
              <Button
                variant="primary"
                loading={apply.isPending}
                onClick={() => {
                  apply.mutate(openPosting.data.id);
                  setOpenId(null);
                }}
              >
                Apply now
              </Button>
            ) : (
              <Button
                variant="secondary"
                onClick={() => {
                  if (openPosting.data !== undefined) {
                    window.open(openPosting.data.url, '_blank', 'noopener,noreferrer');
                  }
                }}
              >
                Open the posting
              </Button>
            )
          }
        >
          {openPosting.data === undefined ? null : (
            <div className="flex flex-col gap-4">
              <div className="flex flex-wrap items-center gap-2">
                <Badge tone={postingStatusTone(openPosting.data.status)}>
                  {postingStatusTone(openPosting.data.status).label}
                </Badge>
                <span className="chip-mono">{formatSalary(openPosting.data)}</span>
                <span className="chip-mono">{humanize(openPosting.data.work_arrangement)}</span>
                <span className="chip-mono">{humanize(openPosting.data.employment_type)}</span>
                {orDash(openPosting.data.location) !== EM_DASH && (
                  <span className="chip-mono">{openPosting.data.location}</span>
                )}
              </div>

              {!canAutoApply(openPosting.data) && (
                <p className="rounded-md border border-st-review/40 bg-st-review/[0.08] p-3 text-sm text-secondary">
                  {providerLabel(openPosting.data.provider)} is discovery-only. Its terms or its
                  sign-in flow make automated submission unsafe, so ApplicantOS will never submit
                  here — open the posting and apply yourself.
                </p>
              )}

              <section>
                <h3 className="label-caps mb-2">Score</h3>
                <ScoreBreakdown
                  score={openPosting.data.score}
                  {...(preferences === undefined ? {} : { threshold: preferences.min_score })}
                />
              </section>

              {openPosting.data.description != null && openPosting.data.description !== '' && (
                <section>
                  <h3 className="label-caps mb-2">Description</h3>
                  <p className="max-w-[760px] whitespace-pre-wrap text-sm text-secondary">
                    {openPosting.data.description}
                  </p>
                </section>
              )}
            </div>
          )}
        </SheetContent>
      </Sheet>
    </Page>
  );
}
