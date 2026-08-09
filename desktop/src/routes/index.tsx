/**
 * The dashboard — the morning read (`docs/UI.md` §8.2).
 *
 * P2: the first screen answers "what happened while I was asleep, and what needs me" without
 * a single click. That is why there is **exactly one hero figure** — what the agent actually
 * did — and why the review card is the only card on the screen allowed an accent bloom, and
 * only while the queue is non-empty.
 *
 * Everything here is derived from data the backend already returns. Where a figure would
 * require an endpoint that does not exist, the panel shows what does exist and says so, rather
 * than inventing a plausible number: the delta on the hero compares the last two *finished*
 * runs, and the top-scoring list is ranked from the postings page already in cache, with its
 * sample stated.
 */

import { DASHBOARD_WINDOW_DAYS } from '@/lib/query/client';
import { ArrowUpRight, Play } from 'lucide-react';
import { useNavigate } from '@tanstack/react-router';
import { useMemo } from 'react';

import {
  ChartCard,
  ActivityChart,
  activitySummary,
  activityTable,
  ACTIVITY_SERIES,
  FunnelBar,
  funnelSummary,
  funnelTable,
} from '@/components/charts';
import { useAppActions } from '@/components/app-actions';
import { Page, SectionHeading } from '@/components/page';
import {
  Badge,
  Button,
  Card,
  CardBody,
  CardHeader,
  EmptyState,
  Kbd,
  ProgressRing,
  ScoreBar,
  StatTile,
  StatusDot,
  Timeline,
  type TimelineItem,
} from '@/components/ui';
import {
  useAnalyticsOverview,
  useApplications,
  usePostings,
  useReviews,
  useSessions,
} from '@/hooks';
import { useDelayedFlag } from '@/hooks/use-delayed-flag';
import { useActiveSession } from '@/hooks/use-sessions';
import type { PostingRead, SessionRead } from '@/lib/api/types';
import {
  cn,
  formatDuration,
  formatRelative,
  formatTime,
  orDash,
  providerLabel,
  reviewReasonLabel,
  statusTone,
} from '@/lib/utils';

/** Days of history behind the dashboard's chart. */
// Shared with the index route loader and the hot snapshot -- see client.ts.
const WINDOW_DAYS = DASHBOARD_WINDOW_DAYS;

/** How many open postings the "worth a look" panel ranks. */
const TOP_POSTINGS = 5;

/** How many entries the recent-activity timeline shows. Bounded by design (§10.11). */
const RECENT_ACTIVITY = 12;

/** Sum the numeric values of a token-usage map. */
function totalTokens(usage: Readonly<Record<string, unknown>>): number {
  return Object.values(usage).reduce<number>(
    (total, value) => total + (typeof value === 'number' ? value : 0),
    0,
  );
}

/** The morning read. */
export function DashboardRoute() {
  const navigate = useNavigate();
  const { startRun } = useAppActions();
  const overview = useAnalyticsOverview(WINDOW_DAYS);
  const sessions = useSessions({ limit: 5 });
  const reviews = useReviews({ limit: 3 });
  const postings = usePostings({ limit: 50 });
  // The same key `/applications` renders from, so the dashboard's activity list and the list
  // screen are one cache entry rather than two that can disagree.
  const applications = useApplications({});
  const activeSession = useActiveSession();

  const showTileSkeletons = useDelayedFlag(overview.isPending);

  // `completed` only — not `!== 'running'`. SessionStatus has four members, so excluding just
  // the running one let a cancelled or failed run be read as a finished one: a run that died
  // three seconds in with nothing discovered reported "finished · 0 applications", which is
  // indistinguishable from an ordinary quiet night. It also poisoned `previousRun`, so the
  // delta showed a -100% collapse in output that never happened.
  const completed = useMemo<SessionRead[]>(
    () => (sessions.data?.items ?? []).filter((session) => session.status === 'completed'),
    [sessions.data],
  );
  const lastRun = completed[0];
  const previousRun = completed[1];

  // A run that ended badly is not hidden — hiding it would be the same lie in the other
  // direction. It is surfaced separately, with its own wording, and only when it is more
  // recent than the last good run.
  const lastEndedBadly = useMemo<SessionRead | undefined>(() => {
    const bad = (sessions.data?.items ?? []).find(
      (session) => session.status === 'failed' || session.status === 'cancelled',
    );
    if (bad === undefined) return undefined;
    if (lastRun === undefined) return bad;
    return Date.parse(bad.started_at) > Date.parse(lastRun.started_at) ? bad : undefined;
  }, [sessions.data, lastRun]);

  // Two honest readings, never one dressed as the other: what the last finished run did, or —
  // when nothing has finished yet — what has been submitted today.
  const hasRun = lastRun !== undefined;
  const heroValue = lastRun?.applications_completed ?? overview.data?.stats.applications_today ?? 0;
  const heroLabel = hasRun
    ? 'applications submitted in the last run'
    : 'applications submitted today';
  const heroDelta =
    lastRun === undefined || previousRun === undefined || previousRun.applications_completed === 0
      ? null
      : (lastRun.applications_completed - previousRun.applications_completed) /
        previousRun.applications_completed;

  const topPostings = useMemo<PostingRead[]>(() => {
    const rows = (postings.data?.items ?? []).filter(
      (posting) => posting.is_open && posting.status !== 'applied' && posting.score != null,
    );
    return [...rows]
      .sort((left, right) => (right.score?.normalized ?? 0) - (left.score?.normalized ?? 0))
      .slice(0, TOP_POSTINGS);
  }, [postings.data]);

  const recentActivity = useMemo<TimelineItem[]>(() => {
    const rows = [...(applications.data?.items ?? [])].sort(
      (left, right) => Date.parse(right.updated_at) - Date.parse(left.updated_at),
    );
    return rows.slice(0, RECENT_ACTIVITY).map((application) => ({
      id: application.id,
      at: application.updated_at,
      title: `${orDash(application.company?.name)} · ${orDash(application.posting?.title)}`,
      status: application.status,
      detail:
        application.review_reason == null
          ? statusTone(application.status).label
          : reviewReasonLabel(application.review_reason),
    }));
  }, [applications.data]);

  const reviewCount = reviews.data?.total ?? 0;
  const stats = overview.data?.stats;
  const timeseries = overview.data?.timeseries ?? [];
  const funnel = overview.data?.funnel ?? [];

  return (
    <Page
      title="Overnight"
      subtitle={
        // A run that was cancelled or failed is reported as exactly that, and it wins the
        // subtitle when it is the most recent thing that happened — being told the run died
        // is the whole point, and it is what the old `!== 'running'` filter concealed.
        lastEndedBadly !== undefined
          ? `Last run ${lastEndedBadly.status === 'failed' ? 'failed' : 'was cancelled'} ${formatTime(lastEndedBadly.ended_at ?? lastEndedBadly.started_at)} after ${formatDuration(lastEndedBadly.duration_seconds)} · ${String(lastEndedBadly.applications_completed)} applications submitted before it stopped`
          : lastRun === undefined
            ? 'No run has finished yet. Everything below fills in after the first one.'
            : `Last run finished ${formatTime(lastRun.ended_at ?? lastRun.started_at)} · ${formatDuration(lastRun.duration_seconds)} · ${String(lastRun.applications_completed)} applications · ${String(lastRun.manual_review)} need review`
      }
      busy={overview.isFetching}
      actions={
        <Button
          variant="primary"
          leadingIcon={<Play aria-hidden="true" />}
          trailingIcon={<Kbd keys={['Ctrl', 'Shift', 'R']} />}
          onClick={startRun}
        >
          Start a run
        </Button>
      }
    >
      <div className="grid grid-cols-12 gap-3">
        {/* The one hero figure: what the agent actually did. */}
        <Card className="col-span-12 flex flex-col justify-center p-5 md:col-span-4">
          <p className="nums-proportional font-mono text-hero font-bold text-primary">
            {heroValue}
          </p>
          <p className="mt-1 text-sm text-secondary">{heroLabel}</p>
          <p className="mt-0.5 text-mini text-muted">
            {heroDelta === null
              ? hasRun
                ? 'No earlier run to compare against yet.'
                : 'No run has finished yet, so this counts today rather than a run.'
              : `${heroDelta >= 0 ? '▲' : '▼'} ${String(Math.abs(Math.round(heroDelta * 100)))}% vs. the previous run`}
          </p>
        </Card>

        <div className="col-span-12 grid grid-cols-2 gap-3 md:col-span-8 lg:grid-cols-4">
          <StatTile
            label="Needs review"
            value={stats?.needs_review ?? 0}
            upIsGood={false}
            loading={showTileSkeletons}
          />
          <StatTile
            label="Interviews"
            value={stats?.interviews ?? 0}
            loading={showTileSkeletons}
          />
          <StatTile
            label="Average score"
            value={
              overview.data?.avg_score === null || overview.data?.avg_score === undefined
                ? '—'
                : Math.round(overview.data.avg_score)
            }
            loading={showTileSkeletons}
          />
          <StatTile
            label="Tokens used"
            value={totalTokens(overview.data?.tokens_used ?? {})}
            upIsGood={false}
            loading={showTileSkeletons}
          />
        </div>

        {activeSession !== null && (
          <Card className="col-span-12">
            <CardHeader
              title={
                <span className="inline-flex items-center gap-2">
                  <StatusDot status="submitting" aria-hidden="true" />
                  Run in progress
                </span>
              }
              subtitle={`started ${formatRelative(activeSession.started_at)} · trigger ${activeSession.trigger}`}
              actions={
                <Button
                  variant="ghost"
                  size="sm"
                  trailingIcon={<ArrowUpRight aria-hidden="true" />}
                  onClick={() => {
                    void navigate({ to: '/sessions' });
                  }}
                >
                  Open runs
                </Button>
              }
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
              <RunCounter label="Found" value={activeSession.jobs_found} />
              <RunCounter label="Qualified" value={activeSession.jobs_qualified} />
              <RunCounter label="Résumés" value={activeSession.resumes_generated} />
              <RunCounter label="Applied" value={activeSession.applications_completed} />
              <RunCounter label="Review" value={activeSession.manual_review} tone="review" />
              <RunCounter label="Failed" value={activeSession.failures} tone="danger" />
            </CardBody>
          </Card>
        )}

        {/* Left column: what needs a human, then what just happened. */}
        <div className="col-span-12 flex flex-col gap-3 lg:col-span-6">
          <Card className={cn(reviewCount > 0 && 'shadow-selected')}>
            <CardHeader
              title="Needs review"
              subtitle={
                reviewCount === 0
                  ? 'Nothing is waiting on you.'
                  : `${String(reviewCount)} blocked ${reviewCount === 1 ? 'application' : 'applications'}`
              }
              actions={
                <Badge tone={statusTone(reviewCount > 0 ? 'needs_review' : 'confirmed')}>
                  {reviewCount}
                </Badge>
              }
            />
            <CardBody>
              {/* §10.10 — "Nothing needs you" is never printed over a queue that has not
                  answered yet, here or on `/reviews`. */}
              {reviews.isPending ? null : reviewCount === 0 ? (
                <EmptyState
                  title="Nothing needs you"
                  description="The agent handled everything on its own."
                />
              ) : (
                <>
                  <ul className="flex flex-col">
                    {(reviews.data?.items ?? []).map((item) => (
                      <li
                        key={item.application.id}
                        className="flex h-11 items-center gap-3 border-b border-state-divider last:border-b-0"
                      >
                        <StatusDot status={item.application.status} aria-hidden="true" />
                        <span className="min-w-0 flex-1 truncate text-sm text-primary">
                          {orDash(item.application.company?.name)} ·{' '}
                          {orDash(item.application.posting?.title)}
                        </span>
                        <span className="shrink-0 text-mini text-st-review">
                          {item.reason === null || item.reason === undefined
                            ? 'Awaiting a decision'
                            : reviewReasonLabel(item.reason)}
                        </span>
                        <span className="shrink-0 font-mono text-mini tabular-nums text-muted">
                          {formatRelative(item.application.updated_at)}
                        </span>
                      </li>
                    ))}
                  </ul>
                  <Button
                    variant="outline-accent"
                    className="mt-3"
                    trailingIcon={<Kbd keys={['g', 'r']} chord />}
                    onClick={() => {
                      void navigate({ to: '/reviews' });
                    }}
                  >
                    Open the review queue
                  </Button>
                </>
              )}
            </CardBody>
          </Card>

          <Card>
            <CardHeader
              title="Worth a look"
              subtitle={`Highest-scoring open postings from the ${String(postings.data?.items.length ?? 0)} most recent`}
            />
            <CardBody>
              {postings.isPending ? null : topPostings.length === 0 ? (
                <EmptyState
                  title="No scored open postings yet"
                  description="Run discovery to fill this list."
                />
              ) : (
                <ul className="flex flex-col">
                  {topPostings.map((posting) => (
                    <li
                      key={posting.id}
                      className="flex h-11 items-center gap-3 border-b border-state-divider last:border-b-0"
                    >
                      <span className="min-w-0 flex-1">
                        <span className="block truncate text-sm text-primary">
                          {posting.title}
                        </span>
                        <span className="block truncate text-mini text-muted">
                          {orDash(posting.company?.name)} · {providerLabel(posting.provider)}
                        </span>
                      </span>
                      <ScoreBar value={posting.score?.normalized ?? 0} className="shrink-0" />
                    </li>
                  ))}
                </ul>
              )}
            </CardBody>
          </Card>

          <section>
            <SectionHeading>Recent activity</SectionHeading>
            {applications.isPending ? null : recentActivity.length === 0 ? (
              <EmptyState
                title="No activity yet"
                description="Applications appear here as the pipeline moves them, newest first."
              />
            ) : (
              <Timeline items={recentActivity} animate={false} />
            )}
          </section>
        </div>

        {/* Right column: the shape of the pipeline. */}
        <div className="col-span-12 flex flex-col gap-3 lg:col-span-6">
          <ChartCard
            title={`Activity · last ${String(WINDOW_DAYS)} days`}
            subtitle="One column per day, by what happened that day"
            summary={activitySummary(timeseries)}
            series={ACTIVITY_SERIES}
            table={activityTable(timeseries)}
            isEmpty={timeseries.every(
              (point) =>
                point.submitted + point.interviews + point.offers + point.rejections === 0,
            )}
            emptyTitle="No activity in this window"
            emptyDescription="Submissions and outcomes appear here the day they happen."
          >
            <ActivityChart points={timeseries} />
          </ChartCard>

          <ChartCard
            title="Funnel"
            subtitle="Discovery through to offer"
            summary={funnelSummary(funnel)}
            table={funnelTable(funnel)}
            isEmpty={funnel.length === 0 || funnel.every((stage) => stage.count === 0)}
            emptyTitle="Nothing has entered the funnel"
            emptyDescription="Discovery is the first stage; run it to populate the rest."
            height={220}
          >
            <FunnelBar stages={funnel} />
          </ChartCard>
        </div>
      </div>
    </Page>
  );
}

/** One counter in the live-run panel. Mono, tabular, and never animated (§P5). */
function RunCounter({
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
          tone === undefined && 'text-primary',
        )}
      >
        {value}
      </span>
    </div>
  );
}
