/**
 * The application detail panel (`docs/UI.md` §8.4).
 *
 * **Tabs are lenses over one object.** Switching never changes the route, never refetches and
 * never loses scroll — a tab that navigates is a tab that costs a paint.
 *
 * **The instant-open rule.** The header paints from whatever the list already had in cache:
 * company, role, status and score are on screen at frame one, and the sections that need the
 * full payload reserve their space with fixed-height placeholders so nothing reflows when it
 * lands. The list row and the detail are two projections of one entity, so the summary is
 * passed in rather than re-fetched — seeding the detail *cache key* with a list DTO would
 * write a partial object under a key that claims to be complete, which is the `initialData`
 * mistake §10.6 warns about.
 *
 * **Every artefact is real or absent.** A confirmation screenshot renders if the pipeline
 * stored one; if it did not, the panel says so rather than showing a placeholder image, which
 * would be a picture of a submission that may never have happened.
 */

import { Download, ExternalLink, RotateCw } from 'lucide-react';
import { useEffect, useMemo, useState } from 'react';

import {
  Badge,
  Button,
  Card,
  CardBody,
  CardHeader,
  EmptyState,
  Skeleton,
  StatusBadge,
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
  Timeline,
  type TimelineItem,
} from '@/components/ui';
import { ScoreBreakdown } from '@/components/score-breakdown';
import { useLogs } from '@/hooks';
import { applicationArtifactUrl } from '@/lib/api/endpoints';
import type {
  ApplicationDetail as ApplicationDetailDto,
  ApplicationRead,
  ArtifactRead,
  JsonObject,
} from '@/lib/api/types';
import {
  cn,
  EM_DASH,
  formatBytes,
  formatDateTime,
  formatDuration,
  formatRelative,
  humanize,
  orDash,
  providerLabel,
  shortId,
  statusTone,
} from '@/lib/utils';

/** Props for {@link ApplicationDetailPanel}. */
export interface ApplicationDetailPanelProps {
  applicationId: string;
  /** The row the list already holds. Paints the header at frame one. */
  summary: ApplicationRead | undefined;
  /** The full payload, once it lands. */
  detail: ApplicationDetailDto | undefined;
  /** True only while the detail query has *never* had data (§10.1). */
  isPending: boolean;
  onRetry?: () => void;
  retrying?: boolean;
  className?: string;
}

/** Everything known about one application. */
export function ApplicationDetailPanel({
  applicationId,
  summary,
  detail,
  isPending,
  onRetry,
  retrying = false,
  className,
}: ApplicationDetailPanelProps) {
  const head = detail ?? summary;

  const timeline = useMemo<TimelineItem[]>(() => {
    if (detail === undefined) return [];
    return detail.events.map((event) => ({
      id: event.id,
      at: event.at,
      title: humanize(event.kind),
      ...(event.message === null || event.message === undefined
        ? {}
        : { detail: event.message }),
    }));
  }, [detail]);

  if (head === undefined) {
    return (
      <div className={cn('flex flex-col gap-3', className)}>
        {isPending ? (
          <>
            <Skeleton className="h-6 w-64" />
            <Skeleton className="h-4 w-80" />
            <Skeleton className="h-64 w-full rounded-lg" />
          </>
        ) : (
          <EmptyState
            title="No such application"
            description="It may have been archived, or this window is holding a link from an older database."
          />
        )}
      </div>
    );
  }

  const company = head.company?.name ?? head.posting?.company?.name;
  const failed = head.status === 'failed';

  return (
    <div className={cn('flex min-h-0 flex-col gap-4', className)}>
      <header className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <h2 className="truncate font-display text-lg font-semibold text-primary">
            {orDash(company)}
            {head.posting?.title === undefined ? '' : ` · ${head.posting.title}`}
          </h2>
          <div className="mt-1 flex flex-wrap items-center gap-2">
            <StatusBadge status={head.status} />
            {head.posting !== null && head.posting !== undefined && (
              <span className="chip-mono">{providerLabel(head.posting.provider)}</span>
            )}
            {head.score !== null && head.score !== undefined && (
              <span className="chip-mono">score {Math.round(head.score.normalized)}</span>
            )}
            <span className="chip-mono">app {shortId(head.id)}</span>
            {head.submitted_at !== null && head.submitted_at !== undefined && (
              <span className="text-mini text-muted">
                submitted {formatRelative(head.submitted_at)}
              </span>
            )}
            {head.duration_seconds !== null && head.duration_seconds !== undefined && (
              <span className="text-mini text-muted">
                took {formatDuration(head.duration_seconds)}
              </span>
            )}
          </div>
        </div>

        <div className="flex shrink-0 items-center gap-2">
          {head.posting !== null && head.posting !== undefined && (
            <Button
              variant="ghost"
              size="sm"
              leadingIcon={<ExternalLink aria-hidden="true" />}
              onClick={() => {
                window.open(head.posting?.url, '_blank', 'noopener,noreferrer');
              }}
            >
              Posting
            </Button>
          )}
          {failed && onRetry !== undefined && (
            <Button
              variant="secondary"
              size="sm"
              loading={retrying}
              leadingIcon={<RotateCw aria-hidden="true" />}
              onClick={onRetry}
            >
              Retry
            </Button>
          )}
        </div>
      </header>

      {head.last_error !== null && head.last_error !== undefined && head.last_error !== '' && (
        <p
          role="alert"
          className="rounded-md border border-st-danger/40 bg-st-danger/[0.10] p-3 font-mono text-mini text-st-danger"
        >
          {head.last_error}
        </p>
      )}

      <Tabs defaultValue="timeline" className="flex min-h-0 flex-1 flex-col">
        <TabsList>
          <TabsTrigger value="timeline">Timeline</TabsTrigger>
          <TabsTrigger value="answers">Answers</TabsTrigger>
          <TabsTrigger value="documents">Documents</TabsTrigger>
          <TabsTrigger value="artifacts">Artifacts</TabsTrigger>
          <TabsTrigger value="activity">Activity</TabsTrigger>
        </TabsList>

        <TabsContent value="timeline" className="flex flex-col gap-4">
          <section>
            <h3 className="label-caps mb-2">Event trail</h3>
            {detail === undefined ? (
              <FixedPlaceholder height={180} pending={isPending} />
            ) : timeline.length === 0 ? (
              <EmptyState
                title="No events recorded"
                description="This application has not moved through the pipeline yet."
              />
            ) : (
              <Timeline items={timeline} animate={false} />
            )}
          </section>

          <section>
            <h3 className="label-caps mb-2">Score breakdown</h3>
            <ScoreBreakdown score={head.score} />
          </section>

          {detail?.ai_reasoning !== null &&
            detail?.ai_reasoning !== undefined &&
            detail.ai_reasoning !== '' && (
              <section>
                <h3 className="label-caps mb-2">Why the agent chose this</h3>
                <p className="text-sm text-secondary">{detail.ai_reasoning}</p>
              </section>
            )}

          {head.notes !== null && head.notes !== undefined && head.notes !== '' && (
            <section>
              <h3 className="label-caps mb-2">Your notes</h3>
              <p className="whitespace-pre-wrap text-sm text-secondary">{head.notes}</p>
            </section>
          )}
        </TabsContent>

        <TabsContent value="answers">
          {detail === undefined ? (
            <FixedPlaceholder height={200} pending={isPending} />
          ) : (
            <AnswersTable answers={detail.answers} />
          )}
        </TabsContent>

        <TabsContent value="documents" className="flex flex-col gap-4">
          {detail === undefined ? (
            <FixedPlaceholder height={220} pending={isPending} />
          ) : (
            <>
              {detail.resume_version === null || detail.resume_version === undefined ? (
                <EmptyState
                  title="No résumé was generated"
                  description="Tailoring happens after scoring. If the application stopped before that, there is nothing here yet."
                />
              ) : (
                <Card>
                  <CardHeader
                    title={`Résumé v${String(detail.resume_version.version_number)}`}
                    subtitle={`${String(detail.resume_version.bullet_count)} bullets · ${String(detail.resume_version.fact_ids.length)} facts · ${detail.resume_version.render_format}`}
                    actions={
                      detail.resume_version.download_url === null ||
                      detail.resume_version.download_url === undefined ? undefined : (
                        <Button
                          variant="secondary"
                          size="sm"
                          leadingIcon={<Download aria-hidden="true" />}
                          onClick={() => {
                            window.open(
                              detail.resume_version?.download_url ?? '',
                              '_blank',
                              'noopener,noreferrer',
                            );
                          }}
                        >
                          Download
                        </Button>
                      )
                    }
                  />
                  <CardBody>
                    <p className="text-sm text-secondary">
                      {detail.resume_version.reasoning ??
                        'Every bullet on this résumé traces to a fact id in your knowledge graph.'}
                    </p>
                  </CardBody>
                </Card>
              )}

              {detail.cover_letter !== null && detail.cover_letter !== undefined && (
                <Card>
                  <CardHeader
                    title="Cover letter"
                    subtitle={`${String(detail.cover_letter.word_count)} words · tone ${detail.cover_letter.tone}`}
                  />
                  <CardBody>
                    <p className="max-w-[760px] whitespace-pre-wrap text-sm text-secondary">
                      {detail.cover_letter.body}
                    </p>
                  </CardBody>
                </Card>
              )}
            </>
          )}
        </TabsContent>

        <TabsContent value="artifacts">
          {detail === undefined ? (
            <FixedPlaceholder height={240} pending={isPending} />
          ) : (
            <ArtifactGrid
              applicationId={applicationId}
              artifacts={detail.artifacts}
              confirmationText={detail.confirmation_text}
              confirmationId={head.confirmation_id}
            />
          )}
        </TabsContent>

        <TabsContent value="activity">
          <ActivityLog applicationId={applicationId} />
        </TabsContent>
      </Tabs>
    </div>
  );
}

/**
 * A fixed-height reservation.
 *
 * Not a skeleton unless the query has genuinely never had data: §7.16 gates skeletons at
 * 400ms, and the shell already occupies this space, so reserving the box is enough to keep
 * the layout-shift budget at zero.
 */
function FixedPlaceholder({ height, pending }: { height: number; pending: boolean }) {
  return (
    <div style={{ height }} aria-hidden="true" className="flex flex-col gap-2">
      {pending && (
        <>
          <Skeleton className="h-4 w-3/4" />
          <Skeleton className="h-4 w-1/2" />
          <Skeleton className="h-4 w-2/3" />
        </>
      )}
    </div>
  );
}

/** The answers the automation and the reviewer supplied, as a two-column table. */
function AnswersTable({ answers }: { answers: JsonObject }) {
  const entries = Object.entries(answers);
  if (entries.length === 0) {
    return (
      <EmptyState
        title="No answers recorded"
        description="Either the form asked nothing beyond the résumé, or the application stopped before the form was reached."
      />
    );
  }

  return (
    <table className="w-full border-collapse text-sm">
      <thead>
        <tr className="border-b border-default">
          <th scope="col" className="pb-1 text-left text-mini font-medium text-muted">
            Field
          </th>
          <th scope="col" className="pb-1 text-left text-mini font-medium text-muted">
            Answer
          </th>
        </tr>
      </thead>
      <tbody>
        {entries.map(([field, value]) => (
          <tr key={field} className="border-b border-state-divider last:border-b-0">
            <td className="w-[40%] py-1.5 pr-4 align-top font-mono text-mini text-secondary">
              {field}
            </td>
            <td className="py-1.5 align-top text-secondary">{renderAnswer(value)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/** Render one answer value without asserting a shape the JSON may not have. */
function renderAnswer(value: unknown): string {
  if (value === null || value === undefined) return EM_DASH;
  if (typeof value === 'string') return value === '' ? EM_DASH : value;
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  if (Array.isArray(value)) return value.map((item) => String(item)).join(', ');
  return JSON.stringify(value);
}

/** Screenshots and stored files, with the confirmation the pipeline actually captured. */
function ArtifactGrid({
  applicationId,
  artifacts,
  confirmationText,
  confirmationId,
}: {
  applicationId: string;
  artifacts: readonly ArtifactRead[];
  confirmationText: string | null | undefined;
  confirmationId: string | null | undefined;
}) {
  const screenshots = artifacts.filter((artifact) => artifact.kind === 'screenshot');
  const others = artifacts.filter((artifact) => artifact.kind !== 'screenshot');

  return (
    <div className="flex flex-col gap-4">
      {(confirmationText !== null && confirmationText !== undefined && confirmationText !== '') ||
      (confirmationId !== null && confirmationId !== undefined) ? (
        <div className="rounded-md border border-st-success/40 bg-st-success/[0.08] p-3">
          <p className="label-caps mb-1">Proof of submission</p>
          {confirmationId !== null && confirmationId !== undefined && (
            <p className="font-mono text-mini text-secondary">confirmation {confirmationId}</p>
          )}
          {confirmationText !== null && confirmationText !== undefined && (
            <p className="text-sm text-secondary">{confirmationText}</p>
          )}
        </div>
      ) : null}

      {screenshots.length === 0 && others.length === 0 ? (
        <EmptyState
          title="No artefacts stored"
          description="Screenshots are captured at submission. An application that has not been submitted has none — which is itself the honest answer."
        />
      ) : (
        <>
          {screenshots.length > 0 && (
            <section>
              <h3 className="label-caps mb-2">Screenshots</h3>
              <div className="grid grid-cols-2 gap-3">
                {screenshots.map((artifact) => (
                  <ArtifactImage
                    key={artifact.id}
                    applicationId={applicationId}
                    artifact={artifact}
                  />
                ))}
              </div>
            </section>
          )}

          {others.length > 0 && (
            <section>
              <h3 className="label-caps mb-2">Files</h3>
              <ul className="flex flex-col">
                {others.map((artifact) => (
                  <li
                    key={artifact.id}
                    className="flex h-8 items-center gap-3 border-b border-state-divider last:border-b-0"
                  >
                    <span className="min-w-0 flex-1 truncate text-sm text-secondary">
                      {artifact.filename}
                    </span>
                    <span className="shrink-0 font-mono text-mini text-muted">
                      {humanize(artifact.kind)}
                    </span>
                    <span className="w-[72px] shrink-0 text-right font-mono text-mini tabular-nums text-muted">
                      {formatBytes(artifact.size_bytes)}
                    </span>
                  </li>
                ))}
              </ul>
            </section>
          )}
        </>
      )}
    </div>
  );
}

/**
 * One screenshot.
 *
 * The absolute URL has to be resolved against the sidecar's runtime port, which is an async
 * call — so the frame declares its dimensions up front and the image fills it when it arrives.
 * Declaring `width`/`height` is what keeps §10.13's layout-shift budget at zero for a grid of
 * images that load at different times.
 */
function ArtifactImage({
  applicationId,
  artifact,
}: {
  applicationId: string;
  artifact: ArtifactRead;
}) {
  const [source, setSource] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    void applicationArtifactUrl(applicationId, artifact.id).then((url) => {
      if (alive) setSource(url);
    });
    return () => {
      alive = false;
    };
  }, [applicationId, artifact.id]);

  return (
    <figure className="overflow-hidden rounded-md border border-default bg-inset">
      <div className="aspect-[16/10] w-full">
        {source !== null && (
          <img
            src={source}
            alt={`Submission screenshot: ${artifact.filename}`}
            className="size-full object-cover object-top"
            loading="lazy"
          />
        )}
      </div>
      <figcaption className="flex items-center justify-between gap-2 px-2 py-1 font-mono text-micro tracking-normal text-muted">
        <span className="truncate">{artifact.filename}</span>
        <span className="shrink-0">{formatDateTime(artifact.created_at)}</span>
      </figcaption>
    </figure>
  );
}

/** Persisted log lines for this application. */
function ActivityLog({ applicationId }: { applicationId: string }) {
  const { data, isPending } = useLogs({ application_id: applicationId, limit: 100 });
  const lines = data?.items ?? [];

  if (lines.length === 0) {
    return isPending ? (
      <FixedPlaceholder height={160} pending />
    ) : (
      <EmptyState
        title="No log lines for this application"
        description="Log retention is bounded, so an old application may have aged out of the store."
      />
    );
  }

  return (
    <ul className="flex flex-col rounded-md border border-default bg-inset p-1 font-mono text-mini">
      {lines.map((line) => (
        <li key={line.id} className="flex h-5 items-center gap-3 px-1">
          <span className="shrink-0 tabular-nums text-muted">
            {formatDateTime(line.at).slice(-5)}
          </span>
          <Badge tone={statusTone(line.level === 'ERROR' ? 'failed' : 'draft')} size="sm">
            {line.level}
          </Badge>
          <span className="min-w-0 flex-1 truncate text-secondary">{line.event}</span>
        </li>
      ))}
    </ul>
  );
}
