/**
 * The knowledge engine (`docs/UI.md` §8.7).
 *
 * Three panes: the sources rail, the canvas, and the inspector. The centre pane toggles
 * between the **entity graph** — canvas, never DOM, because 500 force-laid-out DOM nodes is a
 * guaranteed frame-rate failure — and the **facts table**, which is the keyboard-navigable and
 * screen-readable view of the same knowledge and is therefore not optional.
 *
 * **Editing a fact is a first-class action**, and it is optimistic: `docs/CONTRACTS.md` §8
 * treats a user correction as a *source* of its own, so ticking `verified` is the user
 * teaching the graph rather than patching a record. Failed sources sort first, because a
 * source that did not index is the reason a résumé is missing a bullet.
 */

import { Network, Plus, RotateCw, Trash2 } from 'lucide-react';
import { useMemo, useState } from 'react';

import { GraphCanvas } from '@/components/graph-canvas';
import { Page, ResultCount, SectionHeading, ToolbarSpacer } from '@/components/page';
import {
  Badge,
  Button,
  Checkbox,
  ConfirmDialog,
  DataTable,
  EmptyState,
  Field,
  Input,
  NoResults,
  ProgressRing,
  SearchInput,
  SegmentedControl,
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
import {
  useCreateSource,
  useDeleteSource,
  useFacts,
  useKnowledgeGraph,
  useKnowledgeStats,
  useReindex,
  useSources,
  useUpdateFact,
} from '@/hooks';
import {
  FACT_KINDS,
  SOURCE_KINDS,
  type FactKind,
  type FactRead,
  type GraphNode,
  type SourceKind,
  type SourceRead,
} from '@/lib/api/types';
import {
  cn,
  formatNumber,
  formatRelative,
  humanize,
  indexStatusTone,
  orDash,
  shouldShowEmptyState,
  sourceKindLabel,
} from '@/lib/utils';
import { useSessionStore } from '@/stores/session';
import { useRowHeight } from '@/stores/ui';
import { useFiltersStore } from '@/stores/filters';

/** Graph slice cap. The server caps too; this states the intent at the call site. */
const GRAPH_LIMIT = 500;

/** Views of the same knowledge. */
type KnowledgeView = 'graph' | 'facts';

/** Failed sources first, then the rest by label. */
function sortSources(sources: readonly SourceRead[]): SourceRead[] {
  return [...sources].sort((left, right) => {
    const leftFailed = left.index_status === 'failed' ? 0 : 1;
    const rightFailed = right.index_status === 'failed' ? 0 : 1;
    if (leftFailed !== rightFailed) return leftFailed - rightFailed;
    return (left.label ?? left.uri).localeCompare(right.label ?? right.uri);
  });
}

/** The knowledge screen. */
export function KnowledgeRoute() {
  const [view, setView] = useState<KnowledgeView>('graph');
  const [selectedNode, setSelectedNode] = useState<GraphNode | null>(null);
  const [addOpen, setAddOpen] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState<SourceRead | null>(null);
  const [focusedFact, setFocusedFact] = useState(-1);

  const factFilters = useFiltersStore((state) => state.facts);
  const setFactFilters = useFiltersStore((state) => state.setFacts);
  const clearFilters = useFiltersStore((state) => state.clear);
  const density = useRowHeight();
  const indexing = useSessionStore((state) => state.indexing);

  const sources = useSources({ limit: 100 });
  const stats = useKnowledgeStats();
  const graph = useKnowledgeGraph({ limit: GRAPH_LIMIT });
  const facts = useFacts({ ...factFilters, limit: 200 });
  const reindex = useReindex();
  const createSource = useCreateSource();
  const deleteSource = useDeleteSource();
  const updateFact = useUpdateFact();

  const [newKind, setNewKind] = useState<SourceKind>('github_profile');
  const [newUri, setNewUri] = useState('');
  const [newLabel, setNewLabel] = useState('');

  const rows = sortSources(sources.data?.items ?? []);
  const factRows = useMemo(() => facts.data?.items ?? [], [facts.data]);
  const rawQuery = factFilters.q ?? '';

  const inspectorFacts = useMemo(() => {
    if (selectedNode === null) return [];
    const needle = selectedNode.label.toLowerCase();
    return factRows.filter(
      (fact) =>
        fact.text.toLowerCase().includes(needle) ||
        fact.skills.some((skill) => skill.toLowerCase() === needle) ||
        fact.technologies.some((technology) => technology.toLowerCase() === needle) ||
        (fact.organization ?? '').toLowerCase() === needle,
    );
  }, [factRows, selectedNode]);

  const factColumns = useMemo<DataTableColumn<FactRead>[]>(
    () => [
      {
        id: 'text',
        header: 'Fact',
        width: 'minmax(0, 3fr)',
        cell: (row) => row.text,
      },
      { id: 'kind', header: 'Kind', width: '140px', cell: (row) => humanize(row.kind) },
      {
        id: 'organization',
        header: 'Organization',
        width: '150px',
        cell: (row) => orDash(row.organization),
      },
      {
        id: 'impact',
        header: 'Impact',
        width: '72px',
        align: 'right',
        mono: true,
        cell: (row) => row.impact_score.toFixed(1),
      },
      {
        id: 'confidence',
        header: 'Confidence',
        width: '86px',
        align: 'right',
        mono: true,
        cell: (row) => row.confidence.toFixed(2),
      },
      {
        id: 'verified',
        header: 'Verified',
        width: '76px',
        cell: (row) => (
          <Checkbox
            size="sm"
            checked={row.user_verified}
            aria-label={`Mark "${row.text.slice(0, 40)}" verified`}
            onCheckedChange={(checked) => {
              updateFact.mutate({ id: row.id, body: { user_verified: checked === true } });
            }}
            onClick={(event) => {
              event.stopPropagation();
            }}
          />
        ),
      },
    ],
    [updateFact],
  );

  const knowledge = stats.data;
  const factsEmpty = shouldShowEmptyState(factRows.length === 0, facts.isPending, rawQuery);

  return (
    <Page
      title="Knowledge"
      subtitle={
        knowledge === undefined
          ? 'Indexing state loads from the local cache.'
          : `${String(knowledge.sources)} sources · ${formatNumber(knowledge.documents)} documents · ${formatNumber(knowledge.facts)} facts · ${formatNumber(knowledge.entities)} entities${knowledge.last_indexed_at == null ? '' : ` · indexed ${formatRelative(knowledge.last_indexed_at)}`}`
      }
      busy={graph.isFetching || facts.isFetching}
      bleed
      actions={
        <>
          <Button
            variant="secondary"
            leadingIcon={<RotateCw aria-hidden="true" />}
            loading={reindex.isPending}
            onClick={() => {
              reindex.mutate({});
            }}
          >
            Reindex all
          </Button>
          <Button
            variant="primary"
            leadingIcon={<Plus aria-hidden="true" />}
            onClick={() => {
              setAddOpen(true);
            }}
          >
            Add a source
          </Button>
        </>
      }
      toolbar={
        <>
          <SearchInput
            value={rawQuery}
            placeholder="Search facts…"
            aria-label="Search facts"
            data-page-search
            className="w-60 focus-within:w-80"
            onChange={(event) => {
              setFactFilters({ q: event.target.value });
              setView('facts');
            }}
          />
          <Select
            value={factFilters.kind ?? 'all'}
            onValueChange={(value) => {
              setFactFilters({ kind: value === 'all' ? undefined : (value as FactKind) });
            }}
          >
            <SelectTrigger size="sm" className="w-44" aria-label="Filter facts by kind">
              <SelectValue placeholder="Kind" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">Any kind</SelectItem>
              {FACT_KINDS.map((kind) => (
                <SelectItem key={kind} value={kind}>
                  {humanize(kind)}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <ToolbarSpacer />
          <SegmentedControl
            label="Knowledge view"
            value={view}
            onValueChange={setView}
            options={[
              { value: 'graph', label: 'Graph' },
              { value: 'facts', label: 'Facts' },
            ]}
          />
          <ResultCount count={factRows.length} total={facts.data?.total ?? 0} noun="facts" />
        </>
      }
    >
      <div className="grid h-full min-h-0 grid-cols-12 gap-3">
        {/* Sources rail. */}
        <aside className="scroll-region col-span-12 min-h-0 rounded-lg border border-default bg-surface p-2 lg:col-span-3">
          <SectionHeading>Sources</SectionHeading>
          {rows.length === 0 ? (
            <EmptyState
              title="No sources yet"
              description="ApplicantOS learns from what you point it at. Nothing is scraped."
              primaryAction={{
                label: 'Add a source',
                onClick: () => {
                  setAddOpen(true);
                },
              }}
            />
          ) : (
            <ul className="flex flex-col gap-0.5">
              {rows.map((source) => {
                const tone = indexStatusTone(source.index_status);
                const active = indexing !== null && indexing.sourceId === source.id;
                return (
                  <li
                    key={source.id}
                    className="group flex items-start gap-2 rounded-md px-2 py-1.5 hover:bg-state-hover"
                  >
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-sm text-primary">
                        {orDash(source.label ?? sourceKindLabel(source.kind))}
                      </span>
                      <span className="block truncate font-mono text-micro tracking-normal text-muted">
                        {source.uri}
                      </span>
                      <span className="mt-1 flex items-center gap-2">
                        <Badge tone={tone} size="sm">
                          {tone.label}
                        </Badge>
                        {source.last_indexed_at != null && (
                          <span className="text-micro tracking-normal text-muted">
                            {formatRelative(source.last_indexed_at)}
                          </span>
                        )}
                        {active && (
                          <ProgressRing
                            size="sm"
                            value={indexing.progress ?? 0}
                            label={`Indexing ${source.uri}`}
                          />
                        )}
                      </span>
                      {source.last_error != null && source.last_error !== '' && (
                        <span className="mt-1 block text-micro tracking-normal text-st-danger">
                          {source.last_error}
                        </span>
                      )}
                    </span>
                    <span className="flex shrink-0 items-center gap-0.5 opacity-0 transition-opacity duration-[140ms] group-hover:opacity-100 group-focus-within:opacity-100">
                      <Tooltip content="Reindex this source">
                        <Button
                          variant="ghost"
                          size="sm"
                          icon
                          aria-label={`Reindex ${source.uri}`}
                          onClick={() => {
                            reindex.mutate({ sourceId: source.id, force: true });
                          }}
                        >
                          <RotateCw aria-hidden="true" />
                        </Button>
                      </Tooltip>
                      <Tooltip content="Remove this source and the facts that traced to it">
                        <Button
                          variant="ghost"
                          size="sm"
                          icon
                          aria-label={`Remove ${source.uri}`}
                          onClick={() => {
                            setConfirmDelete(source);
                          }}
                        >
                          <Trash2 aria-hidden="true" />
                        </Button>
                      </Tooltip>
                    </span>
                  </li>
                );
              })}
            </ul>
          )}
        </aside>

        {/* Centre pane. */}
        <div className={cn('col-span-12 min-h-0', selectedNode === null ? 'lg:col-span-9' : 'lg:col-span-6')}>
          {view === 'graph' ? (
            graph.data === undefined || graph.data.nodes.length === 0 ? (
              <EmptyState
                icon={Network}
                title="The graph is empty"
                description="Entities appear once a source has been indexed. Add a source and run an index to populate it."
              />
            ) : (
              <GraphCanvas
                view={graph.data}
                selectedId={selectedNode?.id ?? null}
                onSelect={setSelectedNode}
                className="h-[calc(100vh-260px)]"
              />
            )
          ) : (
            <DataTable
              rows={factRows}
              columns={factColumns}
              label="Knowledge facts"
              rowHeight={density}
              pageSize={50}
              totalCount={facts.data?.total ?? 0}
              focusedIndex={focusedFact}
              onFocusRow={setFocusedFact}
              isPlaceholder={facts.isPlaceholderData}
              className="h-[calc(100vh-260px)]"
              empty={
                !factsEmpty ? null : rawQuery !== '' || factFilters.kind !== undefined ? (
                  <NoResults
                    noun="facts"
                    query={rawQuery}
                    onClear={() => {
                      clearFilters('facts');
                    }}
                  />
                ) : (
                  <EmptyState
                    title="No facts extracted yet"
                    description="Every résumé bullet has to trace to a fact, so nothing can be generated until at least one source has been indexed."
                  />
                )
              }
            />
          )}
        </div>

        {/* Inspector. */}
        {selectedNode !== null && (
          <aside className="scroll-region col-span-12 min-h-0 rounded-lg border border-default bg-surface p-3 lg:col-span-3">
            <div className="flex items-start justify-between gap-2">
              <div className="min-w-0">
                <h2 className="truncate font-display text-md font-semibold text-primary">
                  {selectedNode.label}
                </h2>
                <p className="text-mini text-muted">{humanize(selectedNode.kind)}</p>
              </div>
              <Button
                variant="ghost"
                size="sm"
                onClick={() => {
                  setSelectedNode(null);
                }}
              >
                Close
              </Button>
            </div>

            <dl className="mt-3 flex flex-col gap-1 text-sm">
              <Stat label="Mentions" value={formatNumber(selectedNode.mention_count)} />
              <Stat label="Facts" value={formatNumber(selectedNode.fact_count)} />
              <Stat label="Confidence" value={selectedNode.confidence.toFixed(2)} />
            </dl>

            {selectedNode.summary != null && selectedNode.summary !== '' && (
              <p className="mt-3 text-sm text-secondary">{selectedNode.summary}</p>
            )}

            <SectionHeading className="mt-4">
              Facts mentioning this entity
            </SectionHeading>
            {inspectorFacts.length === 0 ? (
              <p className="text-sm text-muted">
                None in the {formatNumber(factRows.length)} facts currently loaded. Switch to the
                facts view and search for “{selectedNode.label}” to look through the rest.
              </p>
            ) : (
              <ul className="flex flex-col gap-2">
                {inspectorFacts.slice(0, 20).map((fact) => (
                  <li key={fact.id} className="border-b border-state-divider pb-2 last:border-b-0">
                    <p className="text-sm text-secondary">{fact.text}</p>
                    <p className="mt-1 flex items-center gap-2 font-mono text-micro tracking-normal text-muted">
                      <span>impact {fact.impact_score.toFixed(1)}</span>
                      <span>conf {fact.confidence.toFixed(2)}</span>
                      {fact.user_verified && <span className="text-st-success">verified</span>}
                    </p>
                  </li>
                ))}
              </ul>
            )}
          </aside>
        )}
      </div>

      <Sheet open={addOpen} onOpenChange={setAddOpen}>
        <SheetContent
          open={addOpen}
          onOpenChange={setAddOpen}
          title="Add a knowledge source"
          description="ApplicantOS reads what you point it at, and nothing else."
          footer={
            <Button
              variant="primary"
              loading={createSource.isPending}
              disabled={newUri.trim() === ''}
              onClick={() => {
                createSource.mutate(
                  {
                    kind: newKind,
                    uri: newUri.trim(),
                    ...(newLabel.trim() === '' ? {} : { label: newLabel.trim() }),
                  },
                  {
                    onSuccess: () => {
                      setAddOpen(false);
                      setNewUri('');
                      setNewLabel('');
                    },
                  },
                );
              }}
            >
              Add the source
            </Button>
          }
        >
          <div className="flex flex-col gap-4">
            <Field label="Kind" htmlFor="source-kind">
              <Select
                value={newKind}
                onValueChange={(value) => {
                  setNewKind(value as SourceKind);
                }}
              >
                <SelectTrigger id="source-kind" aria-label="Source kind">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {SOURCE_KINDS.map((kind) => (
                    <SelectItem key={kind} value={kind}>
                      {sourceKindLabel(kind)}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </Field>

            <Field
              label="Location"
              help="A URL, a GitHub handle, or an absolute path on this machine."
              htmlFor="source-uri"
            >
              <Input
                id="source-uri"
                mono
                value={newUri}
                placeholder="https://example.com  ·  github.com/you  ·  C:\\projects\\thing"
                onChange={(event) => {
                  setNewUri(event.target.value);
                }}
              />
            </Field>

            <Field label="Label" help="Optional. What to call it in this list." htmlFor="source-label">
              <Input
                id="source-label"
                value={newLabel}
                placeholder="My portfolio"
                onChange={(event) => {
                  setNewLabel(event.target.value);
                }}
              />
            </Field>
          </div>
        </SheetContent>
      </Sheet>

      <ConfirmDialog
        open={confirmDelete !== null}
        onOpenChange={(open) => {
          if (!open) setConfirmDelete(null);
        }}
        title="Remove this source?"
        description={
          <>
            Every fact that traced to <span className="font-mono">{confirmDelete?.uri}</span> is
            removed with it, and a résumé bullet cannot exist without the fact behind it. Re-adding
            the source re-indexes it from scratch.
          </>
        }
        confirmLabel="Remove Source"
        loading={deleteSource.isPending}
        onConfirm={() => {
          const target = confirmDelete;
          setConfirmDelete(null);
          if (target !== null) deleteSource.mutate(target.id);
        }}
      />
    </Page>
  );
}

/** One inspector statistic. */
function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-center justify-between gap-3">
      <dt className="text-secondary">{label}</dt>
      <dd className="font-mono text-mini tabular-nums text-primary">{value}</dd>
    </div>
  );
}
