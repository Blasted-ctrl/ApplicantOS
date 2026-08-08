/**
 * The knowledge engine: sources, facts, entities, the graph, and search.
 *
 * Two things here are consequences of golden rules rather than of the API's shape.
 *
 * **Editing a fact is a first-class action, and it is optimistic.** `docs/CONTRACTS.md` §8
 * treats a user correction as a *source* — `SourceKind.USER_CORRECTION` — so `PATCH
 * /knowledge/facts/{id}` is the user teaching the graph, not patching a record. It is the one
 * knowledge mutation frequent enough to need to feel instant, so it writes the cache first
 * and rolls back on failure.
 *
 * **Reindexing is never optimistic and never assumed.** `fingerprint()` is what makes
 * re-indexing free, so the overwhelmingly common outcome of a reindex is `skipped: true` —
 * nothing changed. Predicting a result locally would show work that did not happen.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import * as api from '@/lib/api/endpoints';
import type {
  EntityFilters,
  FactFilters,
  FactRead,
  FactUpdate,
  GraphFilters,
  PageParams,
  SourceCreate,
  Uuid,
} from '@/lib/api/types';
import { notifyError } from '@/lib/notify';
import { qk } from '@/lib/query/keys';
import {
  entitiesOptions,
  factsOptions,
  knowledgeGraphOptions,
  knowledgeSearchOptions,
  knowledgeSourcesOptions,
  knowledgeStatsOptions,
} from '@/lib/query/options';
import { cancelFor, captureLists, patchListRow, restoreLists } from '@/lib/query/optimistic';

/** `GET /knowledge/sources`. */
export function useSources(params: PageParams = {}) {
  return useQuery(knowledgeSourcesOptions(params));
}

/** `GET /knowledge/facts`. */
export function useFacts(filters: FactFilters = {}) {
  return useQuery(factsOptions(filters));
}

/** `GET /knowledge/entities`. */
export function useEntities(filters: EntityFilters = {}) {
  return useQuery(entitiesOptions(filters));
}

/** `GET /knowledge/graph` — a capped, renderable slice. */
export function useKnowledgeGraph(filters: GraphFilters = {}) {
  return useQuery(knowledgeGraphOptions(filters));
}

/** `GET /knowledge/stats`. */
export function useKnowledgeStats() {
  return useQuery(knowledgeStatsOptions());
}

/** `GET /knowledge/search`. Disabled for an empty query — that is not a request. */
export function useKnowledgeSearch(query: string, options: { kinds?: string; limit?: number } = {}) {
  return useQuery(knowledgeSearchOptions(query, options));
}

/** `POST /knowledge/sources` — register something for the indexer to read. */
export function useCreateSource() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (body: SourceCreate) => api.createSource(body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: qk.knowledge(), refetchType: 'active' });
    },
    onError: (error) => {
      notifyError('Could not add the source', error);
    },
  });
}

/**
 * `DELETE /knowledge/sources/{id}`.
 *
 * Destructive, and the caller confirms first: deleting a source deletes the facts that traced
 * to it, and golden rule #7 means those facts cannot be regenerated from anywhere else.
 */
export function useDeleteSource() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (id: Uuid) => api.deleteSource(id),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: qk.knowledge(), refetchType: 'active' });
    },
    onError: (error) => {
      notifyError('Could not remove the source', error);
    },
  });
}

/**
 * `POST /knowledge/sources/{id}/reindex`, or `POST /knowledge/reindex` for everything.
 *
 * Answered 202. Progress arrives as `knowledge.index_*` frames and drives the ProgressRing
 * through the session store — nothing is written into the cache here, because a percentage is
 * not an entity.
 */
export function useReindex() {
  return useMutation({
    mutationFn: ({ sourceId, force = false }: { sourceId?: Uuid; force?: boolean }) =>
      sourceId === undefined ? api.reindexAll(force) : api.reindexSource(sourceId, force),
    onError: (error) => {
      notifyError('Could not start indexing', error);
    },
  });
}

/**
 * `PATCH /knowledge/facts/{id}` — optimistic, with rollback.
 *
 * Marking a fact verified or correcting its text is the most frequent knowledge mutation and
 * the one whose latency the user would feel, so the cache is written first. The old row is
 * captured at mutation time; without it a failure would leave the corrected text on screen
 * with nothing behind it.
 */
export function useUpdateFact() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({ id, body }: { id: Uuid; body: FactUpdate }) => api.updateFact(id, body),

    onMutate: async ({ id, body }) => {
      const prefix = [...qk.knowledge(), 'facts'] as const;
      await cancelFor([prefix]);
      const lists = captureLists(prefix);

      const patch: Partial<FactRead> = {};
      for (const [key, value] of Object.entries(body)) {
        if (value !== undefined && value !== null) {
          (patch as Record<string, unknown>)[key] = value;
        }
      }
      patchListRow<FactRead>(prefix, id, patch);
      return { lists };
    },

    onError: (error, _variables, context) => {
      restoreLists(context?.lists);
      notifyError('Could not update the fact', error);
    },

    // Editing a fact can change the stats (verified counts) and the graph, so both are
    // refreshed once the write has settled rather than predicted.
    onSettled: () => {
      void queryClient.invalidateQueries({
        queryKey: qk.knowledgeStats(),
        refetchType: 'active',
      });
    },
  });
}
