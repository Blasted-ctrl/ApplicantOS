/**
 * Résumés (`docs/UI.md` §8.8).
 *
 * Golden rule #6 shapes this whole screen: a résumé is a **generated view** over the knowledge
 * graph. A variant is configuration, a version is an immutable generation, and
 * `content_json` — not the PDF — is the artefact that is kept forever. So the preview renders
 * the structure and the file is a download, and every bullet carries the `fact_id` it traces
 * to (golden rule #7), which is what makes "nothing is fabricated" checkable by the person it
 * matters to rather than only by the test suite.
 *
 * The preview is one of the two `React.lazy` leaves in the renderer (§10.7). Everything else
 * ships in one chunk; this one is split because most sessions never open it.
 */

import { Download, Plus } from 'lucide-react';
import { Suspense, lazy, useEffect, useMemo, useState } from 'react';

import { Page, SectionHeading } from '@/components/page';
import {
  Badge,
  Button,
  Card,
  CardBody,
  CardHeader,
  EmptyState,
  Field,
  Input,
  Kbd,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  Sheet,
  SheetContent,
  Skeleton,
  Tooltip,
} from '@/components/ui';
import { useCreateResume, usePostings, usePreviewResume, useResumeVersion, useResumes } from '@/hooks';
import { downloadResumeVersion } from '@/lib/api/endpoints';
import { notifyError, notifyFileWritten } from '@/lib/notify';
import type { ResumeRead } from '@/lib/api/types';
import { cn, formatRelative, orDash, statusTone } from '@/lib/utils';

/** The lazily-loaded preview. */
const ResumeDocument = lazy(() => import('@/components/resume-document'));

/** Templates the document pipeline ships. */
const TEMPLATES = ['modern', 'classic', 'ats_plain'] as const;

/** Hand a rendered file to the operating system. */
async function saveVersion(versionId: string, filename: string): Promise<void> {
  const blob = await downloadResumeVersion(versionId);
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  URL.revokeObjectURL(url);
}

/** The résumé library. */
export function ResumesRoute() {
  const resumes = useResumes({ limit: 50 });
  const createResume = useCreateResume();
  const preview = usePreviewResume();
  const postings = usePostings({ limit: 50 });

  const variants = useMemo(() => resumes.data?.items ?? [], [resumes.data]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [versionId, setVersionId] = useState<string | null>(null);
  const [newOpen, setNewOpen] = useState(false);
  const [newName, setNewName] = useState('');
  const [newTemplate, setNewTemplate] = useState<string>('modern');
  const [previewPostingId, setPreviewPostingId] = useState<string>('');

  useEffect(() => {
    setSelectedId((current) => {
      if (current !== null && variants.some((variant) => variant.id === current)) return current;
      return variants[0]?.id ?? null;
    });
  }, [variants]);

  const selected: ResumeRead | undefined = variants.find((variant) => variant.id === selectedId);
  const activeVersionId = versionId ?? selected?.latest_version?.id ?? null;
  const version = useResumeVersion(activeVersionId ?? undefined);
  // Bound to a const so the narrowing survives into the download callback: TypeScript resets
  // narrowing on a mutable property access the moment it crosses a closure boundary.
  const shown = version.data;

  const totalVersions = variants.reduce((total, variant) => total + variant.version_count, 0);
  const defaultVariant = variants.find((variant) => variant.is_default);

  return (
    <Page
      title="Résumés"
      subtitle={
        variants.length === 0
          ? 'No variants yet. A variant is a named configuration; its versions are what get generated.'
          : `${String(variants.length)} ${variants.length === 1 ? 'variant' : 'variants'} · ${String(totalVersions)} versions${defaultVariant === undefined ? '' : ` · default “${defaultVariant.name}”`}`
      }
      busy={resumes.isFetching}
      actions={
        <Button
          variant="primary"
          leadingIcon={<Plus aria-hidden="true" />}
          onClick={() => {
            setNewOpen(true);
          }}
        >
          New variant
        </Button>
      }
    >
      {variants.length === 0 ? (
        <EmptyState
          title="No résumé variants yet"
          description="A variant is a named configuration — a template and a slant. Versions are generated from your knowledge graph each time a posting is tailored for."
          primaryAction={{
            label: 'Create a variant',
            onClick: () => {
              setNewOpen(true);
            },
          }}
        />
      ) : (
        <div className="grid grid-cols-12 gap-4">
          <aside className="col-span-12 flex flex-col gap-4 lg:col-span-4">
            <section>
              <SectionHeading>Variants</SectionHeading>
              <ul className="flex flex-col">
                {variants.map((variant) => (
                  <li key={variant.id}>
                    <button
                      type="button"
                      onClick={() => {
                        setSelectedId(variant.id);
                        setVersionId(null);
                      }}
                      className={cn(
                        'flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left',
                        'transition-colors duration-[140ms] ease-out-quad hover:bg-state-hover',
                        variant.id === selectedId && 'bg-accent-subtle',
                      )}
                    >
                      <span className="min-w-0 flex-1">
                        <span className="block truncate text-sm text-primary">{variant.name}</span>
                        <span className="block truncate text-mini text-muted">
                          {variant.template} · {variant.version_count}{' '}
                          {variant.version_count === 1 ? 'version' : 'versions'}
                        </span>
                      </span>
                      {variant.is_default && (
                        <Badge tone={statusTone('confirmed')} size="sm">
                          default
                        </Badge>
                      )}
                    </button>
                  </li>
                ))}
              </ul>
            </section>

            {selected?.latest_version !== null && selected?.latest_version !== undefined && (
              <section>
                <SectionHeading>Latest version</SectionHeading>
                <button
                  type="button"
                  onClick={() => {
                    setVersionId(selected.latest_version?.id ?? null);
                  }}
                  className={cn(
                    'flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left',
                    'transition-colors duration-[140ms] ease-out-quad hover:bg-state-hover',
                    activeVersionId === selected.latest_version.id && 'bg-accent-subtle',
                  )}
                >
                  <span className="min-w-0 flex-1">
                    <span className="block font-mono text-sm text-primary">
                      v{selected.latest_version.version_number}
                    </span>
                    <span className="block truncate text-mini text-muted">
                      {formatRelative(selected.latest_version.created_at)} ·{' '}
                      {selected.latest_version.bullet_count} bullets
                    </span>
                  </span>
                </button>
                <p className="mt-2 text-mini text-muted">
                  Older versions are addressable by id and are kept forever, but the list endpoint
                  returns only the latest per variant — so only that one is offered here rather
                  than a list this client cannot actually fetch.
                </p>
              </section>
            )}
          </aside>

          <section className="col-span-12 flex flex-col gap-4 lg:col-span-8">
            {shown === undefined ? (
              version.isPending && activeVersionId !== null ? (
                <div className="flex flex-col gap-2">
                  <Skeleton className="h-6 w-48" />
                  <Skeleton className="h-64 w-full rounded-lg" />
                </div>
              ) : (
                <EmptyState
                  title="This variant has never been generated"
                  description="A version is produced the first time a posting is tailored for with this variant, or from a preview."
                />
              )
            ) : (
              <>
                <Card>
                  <CardHeader
                    title={`${orDash(selected?.name)} · v${String(shown.version_number)}`}
                    subtitle={`${String(shown.bullet_count)} bullets · ${String(shown.fact_ids.length)} facts · ${shown.render_format} · ${formatRelative(shown.created_at)}`}
                    actions={
                      shown.has_artifact ? (
                        <Button
                          variant="secondary"
                          size="sm"
                          leadingIcon={<Download aria-hidden="true" />}
                          onClick={() => {
                            const id = shown.id;
                            void saveVersion(
                              id,
                              `resume-v${String(shown.version_number)}.${shown.render_format}`,
                            )
                              .then(() => {
                                notifyFileWritten(
                                  `resume-v${String(shown.version_number)}.${shown.render_format}`,
                                );
                              })
                              .catch((error: unknown) => {
                                notifyError('Could not download the résumé', error);
                              });
                          }}
                        >
                          Download
                        </Button>
                      ) : (
                        <Tooltip content="The rendered file has been cleaned up. The structured version below is the permanent record.">
                          <span className="chip-mono">file deleted</span>
                        </Tooltip>
                      )
                    }
                  />
                  <CardBody className="flex flex-col gap-3">
                    <div>
                      <h3 className="label-caps mb-1.5">
                        Facts used ({shown.fact_ids.length})
                      </h3>
                      {shown.fact_ids.length === 0 ? (
                        <p className="text-sm text-muted">
                          No fact ids recorded on this version.
                        </p>
                      ) : (
                        <div className="flex flex-wrap gap-1.5">
                          {shown.fact_ids.map((factId) => (
                            <span key={factId} className="chip-mono" title={factId}>
                              {factId.slice(0, 8)}
                            </span>
                          ))}
                        </div>
                      )}
                    </div>

                    {shown.reasoning != null && shown.reasoning !== '' && (
                      <div>
                        <h3 className="label-caps mb-1">Reasoning</h3>
                        <p className="text-sm text-secondary">{shown.reasoning}</p>
                      </div>
                    )}
                  </CardBody>
                </Card>

                <Suspense
                  fallback={<Skeleton className="h-96 w-full max-w-[760px] rounded-lg" />}
                >
                  <ResumeDocument document={shown.content_json} />
                </Suspense>
              </>
            )}
          </section>
        </div>
      )}

      <Sheet open={newOpen} onOpenChange={setNewOpen}>
        <SheetContent
          open={newOpen}
          onOpenChange={setNewOpen}
          title="New résumé variant"
          description="A variant is configuration. Its content is generated from your knowledge graph."
          footer={
            <>
              <Button
                variant="secondary"
                loading={preview.isPending}
                disabled={previewPostingId === ''}
                onClick={() => {
                  preview.mutate(
                    {
                      posting_id: previewPostingId,
                      template: newTemplate,
                      render: false,
                    },
                    {
                      onSuccess: (generated) => {
                        setVersionId(generated.id);
                        setNewOpen(false);
                      },
                    },
                  );
                }}
              >
                Preview against a posting
              </Button>
              <Button
                variant="primary"
                loading={createResume.isPending}
                disabled={newName.trim() === ''}
                trailingIcon={<Kbd keys={['Ctrl', 'Enter']} />}
                onClick={() => {
                  createResume.mutate(
                    { name: newName.trim(), template: newTemplate },
                    {
                      onSuccess: (created) => {
                        setSelectedId(created.id);
                        setNewOpen(false);
                        setNewName('');
                      },
                    },
                  );
                }}
              >
                Create the variant
              </Button>
            </>
          }
        >
          <div className="flex flex-col gap-4">
            <Field label="Name" htmlFor="variant-name">
              <Input
                id="variant-name"
                value={newName}
                placeholder="Backend — modern"
                onChange={(event) => {
                  setNewName(event.target.value);
                }}
              />
            </Field>

            <Field label="Template" htmlFor="variant-template">
              <Select value={newTemplate} onValueChange={setNewTemplate}>
                <SelectTrigger id="variant-template" aria-label="Template">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {TEMPLATES.map((template) => (
                    <SelectItem key={template} value={template}>
                      {template}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </Field>

            <Field
              label="Preview against"
              help="Generating a preview costs tokens and produces a real version."
              htmlFor="variant-posting"
            >
              <Select value={previewPostingId} onValueChange={setPreviewPostingId}>
                <SelectTrigger id="variant-posting" aria-label="Posting to tailor against">
                  <SelectValue placeholder="Choose a posting" />
                </SelectTrigger>
                <SelectContent>
                  {(postings.data?.items ?? []).slice(0, 30).map((posting) => (
                    <SelectItem key={posting.id} value={posting.id}>
                      {posting.title} · {orDash(posting.company?.name)}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </Field>
          </div>
        </SheetContent>
      </Sheet>
    </Page>
  );
}
