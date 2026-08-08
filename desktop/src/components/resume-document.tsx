/**
 * The résumé preview — one of the two lazily-loaded leaves in the renderer (`docs/UI.md`
 * §8.8, §10.7).
 *
 * It renders `ResumeVersion.content_json`, **not** the PDF. Two reasons, and the first is the
 * product's:
 *
 * Golden rule #6 — a résumé is a *generated view* over the knowledge graph. `content_json` is
 * the permanent artefact and the rendered file is disposable, so previewing the structure is
 * previewing the thing that actually exists. It is also the only form in which every bullet
 * can carry its `fact_ids`, which golden rule #7 requires and which a flattened PDF destroys.
 *
 * The second is the shell's: the production CSP ships `frame-src 'none'` and `object-src
 * 'none'`, so an embedded PDF viewer is not merely heavy here, it is unreachable. `Download`
 * hands the file to the operating system, which is where a PDF belongs.
 *
 * Default-exported so `React.lazy` can consume it directly.
 */

import type { ResumeDocumentSchema } from '@/lib/api/types';
import { cn, orDash } from '@/lib/utils';

/** Props for {@link ResumeDocument}. */
export interface ResumeDocumentProps {
  document: ResumeDocumentSchema;
  /** Fact ids the user has clicked into, highlighted so provenance is traceable both ways. */
  highlightedFactIds?: ReadonlySet<string>;
  onSelectFact?: (factId: string) => void;
  className?: string;
}

/** The structured résumé, laid out as the page it will become. */
export function ResumeDocument({
  document: content,
  highlightedFactIds,
  onSelectFact,
  className,
}: ResumeDocumentProps) {
  const contact = content.contact;
  const links = Object.entries(contact.links);

  return (
    <article
      className={cn(
        'mx-auto w-full max-w-[760px] rounded-lg border border-default bg-surface p-8',
        className,
      )}
    >
      <header className="border-b border-state-divider pb-3">
        <h3 className="font-display text-lg font-semibold text-primary">{orDash(contact.name)}</h3>
        <p className="mt-1 flex flex-wrap gap-x-3 gap-y-1 font-mono text-mini text-secondary">
          {[contact.email, contact.phone, contact.location, contact.website]
            .filter((value): value is string => value !== null && value !== undefined && value !== '')
            .map((value) => (
              <span key={value}>{value}</span>
            ))}
          {links.map(([label, href]) => (
            <span key={label}>
              {label}: {href}
            </span>
          ))}
        </p>
      </header>

      {content.summary !== null && content.summary !== undefined && content.summary !== '' && (
        <p className="mt-4 text-sm text-secondary">{content.summary}</p>
      )}

      {content.sections.map((section) => (
        <section key={section.heading} className="mt-5">
          <h4 className="label-caps mb-2">{section.heading}</h4>
          <div className="flex flex-col gap-3">
            {section.entries.map((entry, entryIndex) => (
              <div key={`${entry.title}-${String(entryIndex)}`}>
                <div className="flex flex-wrap items-baseline justify-between gap-2">
                  <p className="text-sm font-medium text-primary">
                    {entry.title}
                    {entry.organization === null || entry.organization === undefined
                      ? ''
                      : ` · ${entry.organization}`}
                  </p>
                  <p className="font-mono text-mini tabular-nums text-muted">
                    {orDash(entry.date_range)}
                    {entry.location === null || entry.location === undefined
                      ? ''
                      : ` · ${entry.location}`}
                  </p>
                </div>
                <ul className="mt-1 flex flex-col gap-0.5">
                  {entry.bullets.map((bullet, bulletIndex) => {
                    const factId = entry.fact_ids[bulletIndex];
                    const highlighted =
                      factId !== undefined && highlightedFactIds?.has(factId) === true;
                    return (
                      <li
                        key={`${String(bulletIndex)}-${bullet.slice(0, 24)}`}
                        className={cn(
                          'flex gap-2 rounded-xs pl-1 text-sm text-secondary',
                          highlighted && 'bg-accent-subtle',
                        )}
                      >
                        <span aria-hidden="true" className="select-none text-muted">
                          ·
                        </span>
                        <span className="min-w-0 flex-1">{bullet}</span>
                        {factId !== undefined && (
                          <button
                            type="button"
                            onClick={() => {
                              onSelectFact?.(factId);
                            }}
                            title={`Traces to knowledge fact ${factId}`}
                            className="chip-mono shrink-0 self-start hover:border-accent-border hover:text-accent-text"
                          >
                            fact
                          </button>
                        )}
                      </li>
                    );
                  })}
                </ul>
              </div>
            ))}
          </div>
        </section>
      ))}

      {content.skills_line !== null &&
        content.skills_line !== undefined &&
        content.skills_line !== '' && (
          <section className="mt-5">
            <h4 className="label-caps mb-1">Skills</h4>
            <p className="text-sm text-secondary">{content.skills_line}</p>
          </section>
        )}
    </article>
  );
}

export default ResumeDocument;
