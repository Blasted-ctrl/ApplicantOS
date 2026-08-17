/**
 * The score breakdown (`docs/UI.md` §8.4, §7.19).
 *
 * A score is the reason an application exists at all, so the arithmetic is shown rather than
 * summarised: every rule that fired, what it contributed, and the model's own rationale in its
 * own words. Hard negatives are called out because they do not merely subtract — they zero the
 * score outright, and a reader who cannot see that will not understand why an otherwise strong
 * posting was skipped.
 *
 * All the mini bars share one scale (§7.19), so their lengths are comparable. A breakdown
 * where each row normalises to its own width looks tidier and says nothing about which rule
 * actually decided the outcome.
 */

import { EmptyState, ScoreBar, ScoreComponentRow } from '@/components/ui';
import type { MatchBreakdown, ScoreRead } from '@/lib/api/types';
import { cn, scoreVerdict } from '@/lib/utils';

/**
 * Read the match block out of a score's raw breakdown.
 *
 * `breakdown` is a `JsonObject` because the server stores the engine's own output verbatim,
 * so this is the one place the shape is asserted — narrowed on the fields actually read
 * rather than cast wholesale, so an older score written before matching existed returns
 * `null` instead of rendering four `undefined`s as zeroes.
 */
function readMatch(score: ScoreRead): MatchBreakdown | null {
  const raw = (score.breakdown as { match?: unknown }).match;
  if (raw === null || typeof raw !== 'object') return null;
  const candidate = raw as Partial<MatchBreakdown>;
  if (typeof candidate.combined !== 'number' || typeof candidate.percent !== 'object') return null;
  return candidate as MatchBreakdown;
}

/** Props for {@link ScoreBreakdown}. */
export interface ScoreBreakdownProps {
  score: ScoreRead | null | undefined;
  /** `preferences.min_score`, drawn as the bar's threshold tick. */
  threshold?: number;
  className?: string;
}

/** The score, its components, and the rationale behind it. */
export function ScoreBreakdown({ score, threshold, className }: ScoreBreakdownProps) {
  if (score === null || score === undefined) {
    return (
      <EmptyState
        title="Not scored yet"
        description="This posting has not been through the scoring pass. Scores appear as soon as the run reaches it."
      />
    );
  }

  const components = score.components.filter((component) => component.matched || component.points !== 0);
  const scale = components.reduce(
    (largest, component) => Math.max(largest, Math.abs(component.points)),
    0,
  );

  return (
    <div className={cn('flex flex-col gap-3', className)}>
      {/* `fluid` makes the track fill its box, so the number and the verdict word are laid
          out here rather than inside the bar — a `w-full` track and an inline value are the
          same row and would sit on top of each other. */}
      <div className="flex items-center gap-3">
        <ScoreBar
          value={score.normalized}
          size="lg"
          fluid
          hideValue
          className="min-w-0 flex-1"
          {...(threshold === undefined ? {} : { threshold })}
        />
        <span className="shrink-0 font-mono text-md tabular-nums text-primary">
          {Math.round(score.normalized)}
        </span>
        <span className="w-[72px] shrink-0 text-sm text-secondary">
          {scoreVerdict(score.normalized)}
        </span>
      </div>

      {components.length === 0 ? (
        <p className="text-sm text-muted">
          No rule contributed to this score. The rule pack matched nothing on this posting.
        </p>
      ) : (
        <div className="grid grid-cols-1 gap-x-8 md:grid-cols-2">
          {components.map((component) => (
            <ScoreComponentRow
              key={component.key}
              label={component.label}
              points={component.points}
              scale={scale}
              hardNegative={component.hard_negative}
              detail={component.detail}
            />
          ))}
        </div>
      )}

      <WhyThisMatched match={readMatch(score)} />

      {score.rationale !== null && score.rationale !== undefined && score.rationale !== '' && (
        <p className="text-sm text-secondary">{score.rationale}</p>
      )}

      <p className="font-mono text-micro tracking-normal text-muted">
        {score.model_used === null || score.model_used === undefined
          ? 'scored by the deterministic rule pack'
          : `scored by ${score.model_used}`}
        {' · total '}
        {score.total.toFixed(1)}
        {score.verdict === null || score.verdict === undefined ? '' : ` · verdict ${score.verdict}`}
      </p>
    </div>
  );
}

/**
 * "Why this job matched" — the three signals behind the match, and the skills gap.
 *
 * Rendered as three labelled bars on one shared scale rather than as prose, because the
 * question a reader is actually asking is comparative: *which* of the three carried this
 * posting, and which one let it down. Prose can state three numbers; only a shared scale
 * lets you see at a glance that the title was perfect and the skills were not.
 *
 * The missing skills are the half a user can act on, so they are shown even though the
 * matched ones are the flattering list.
 */
function WhyThisMatched({ match }: { match: MatchBreakdown | null }) {
  if (match === null) return null;

  if (!match.comparable) {
    return (
      <p className="text-sm text-muted">
        Not enough text to compare this posting to your background. Index a résumé or a
        project so future postings can be matched against it.
      </p>
    );
  }

  const rows = [
    { label: 'Title relevance', value: match.percent.title_relevance },
    { label: 'Skills overlap', value: match.percent.skills_overlap },
    { label: 'Résumé similarity', value: match.percent.resume_similarity },
  ];

  return (
    <section className="flex flex-col gap-2 rounded-md border border-default bg-inset p-3">
      <div className="flex items-baseline justify-between">
        <h4 className="label-caps">Why this job matched</h4>
        <span className="font-mono text-md tabular-nums text-primary">
          {match.percent.combined}%
        </span>
      </div>

      <div className="flex flex-col gap-1.5">
        {rows.map((row) => (
          <div key={row.label} className="flex items-center gap-3">
            <span className="w-36 shrink-0 text-sm text-secondary">{row.label}</span>
            <ScoreBar value={row.value} size="sm" fluid hideValue className="min-w-0 flex-1" />
            <span className="w-10 shrink-0 text-right font-mono text-mini tabular-nums text-secondary">
              {row.value}%
            </span>
          </div>
        ))}
      </div>

      {match.missing_skills.length > 0 && (
        <p className="text-mini text-muted">
          <span className="text-secondary">Asks for, and you have not indexed: </span>
          {match.missing_skills.join(', ')}
        </p>
      )}
    </section>
  );
}
