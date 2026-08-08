/**
 * The applications family: list, detail, artifacts, and the mutations that move an
 * application's status.
 *
 * Which mutations may be optimistic is not a style question here (`docs/UI.md` §10.9):
 *
 * - `PATCH /applications/{id}` **is** optimistic. Status and notes are local bookkeeping; if
 *   the write fails, putting the old value back costs the user nothing.
 * - `POST /applications/{id}/retry` **is not**. It re-drives a real browser flow. It flips to
 *   `preparing` locally and waits for the pipeline's own `application.*` frame.
 *
 * Archiving is optimistic *and* undoable, which is the combination §7.15 case 2 exists for:
 * the row leaves the list on the same frame as the click, and the toast holds the reversal
 * for eight seconds.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import * as api from '@/lib/api/endpoints';
import type {
  ApplicationDetail,
  ApplicationFilters,
  ApplicationRead,
  ApplicationStatus,
  ApplicationUpdate,
  Uuid,
} from '@/lib/api/types';
import { notifyError, notifyUndo } from '@/lib/notify';
import { qk } from '@/lib/query/keys';
import {
  applicationArtifactsOptions,
  applicationDetailOptions,
  applicationListOptions,
} from '@/lib/query/options';
import {
  cancelFor,
  captureLists,
  patchListRow,
  removeListRow,
  restoreLists,
  type ListSnapshot,
} from '@/lib/query/optimistic';

/** `GET /applications`. Patched in place by every `application.*` frame — never refetched. */
export function useApplications(filters: ApplicationFilters = {}) {
  return useQuery(applicationListOptions(filters));
}

/** `GET /applications/{id}`. */
export function useApplication(id: Uuid | undefined) {
  return useQuery({
    ...applicationDetailOptions(id ?? ''),
    enabled: id !== undefined && id !== '',
  });
}

/** `GET /applications/{id}/artifacts`. */
export function useApplicationArtifacts(id: Uuid | undefined) {
  return useQuery({
    ...applicationArtifactsOptions(id ?? ''),
    enabled: id !== undefined && id !== '',
  });
}

/** What `onMutate` hands to `onError` so a failure can put the old values back. */
interface RollbackContext {
  detail: ApplicationDetail | undefined;
  lists: ListSnapshot[];
}

/**
 * `PATCH /applications/{id}` — optimistic, with rollback.
 *
 * The old value is captured *at mutation time*. Without that capture this would not be
 * optimistic UI, it would be a race: `onError` would have nothing to restore and the failed
 * value would stay on screen looking successful.
 */
export function useUpdateApplication(id: Uuid) {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (body: ApplicationUpdate) => api.updateApplication(id, body),

    onMutate: async (body): Promise<RollbackContext> => {
      await cancelFor([qk.applicationDetail(id), qk.applicationLists()]);

      const detail = queryClient.getQueryData<ApplicationDetail>(qk.applicationDetail(id));
      const lists = captureLists(qk.applicationLists());

      const patch: Partial<ApplicationRead> = {};
      if (body.status !== undefined && body.status !== null) patch.status = body.status;
      if (body.notes !== undefined) patch.notes = body.notes;

      queryClient.setQueryData<ApplicationDetail>(qk.applicationDetail(id), (old) =>
        old === undefined ? undefined : { ...old, ...patch },
      );
      patchListRow<ApplicationRead>(qk.applicationLists(), id, patch);

      return { detail, lists };
    },

    onError: (error, _body, context) => {
      queryClient.setQueryData(qk.applicationDetail(id), context?.detail);
      restoreLists(context?.lists);
      notifyError('Could not update the application', error);
    },

    // The server may have derived more than was asked for — `can_submit` flips with the
    // status — so the authoritative row is fetched once the write has settled.
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: qk.applicationDetail(id) });
    },
  });
}

/**
 * Move an application to a new status. The `s` row action and the detail view's Select.
 *
 * Separate from {@link useUpdateApplication} because it is bound to a row rather than to an
 * open detail view, so the id arrives with the call.
 */
export function useSetApplicationStatus() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({ id, status }: { id: Uuid; status: ApplicationStatus }) =>
      api.updateApplication(id, { status }),

    onMutate: async ({ id, status }): Promise<RollbackContext> => {
      await cancelFor([qk.applicationDetail(id), qk.applicationLists()]);
      const detail = queryClient.getQueryData<ApplicationDetail>(qk.applicationDetail(id));
      const lists = captureLists(qk.applicationLists());
      queryClient.setQueryData<ApplicationDetail>(qk.applicationDetail(id), (old) =>
        old === undefined ? undefined : { ...old, status },
      );
      patchListRow<ApplicationRead>(qk.applicationLists(), id, { status });
      return { detail, lists };
    },

    onError: (error, { id }, context) => {
      queryClient.setQueryData(qk.applicationDetail(id), context?.detail);
      restoreLists(context?.lists);
      notifyError('Could not change the status', error);
    },

    onSettled: (_data, _error, { id }) => {
      void queryClient.invalidateQueries({ queryKey: qk.applicationDetail(id) });
    },
  });
}

/**
 * Archive an application — optimistic removal with an undo.
 *
 * `abandoned` is a terminal status, so this is destructive in the §9.5 sense and its binding
 * is `Ctrl+X` with a confirmation. The undo path re-issues the previous status rather than
 * restoring the cache, because by the time the user presses it the server has already moved.
 */
export function useArchiveApplication() {
  const queryClient = useQueryClient();
  const setStatus = useSetApplicationStatus();

  return useMutation({
    mutationFn: (application: ApplicationRead) =>
      api.updateApplication(application.id, { status: 'abandoned' }),

    onMutate: async (application): Promise<RollbackContext> => {
      await cancelFor([qk.applicationDetail(application.id), qk.applicationLists()]);
      const detail = queryClient.getQueryData<ApplicationDetail>(
        qk.applicationDetail(application.id),
      );
      const lists = captureLists(qk.applicationLists());
      removeListRow<ApplicationRead>(qk.applicationLists(), application.id);
      return { detail, lists };
    },

    onError: (error, _application, context) => {
      restoreLists(context?.lists);
      notifyError('Could not archive the application', error);
    },

    onSuccess: (_data, application) => {
      const previous = application.status;
      notifyUndo('Application archived', () => {
        setStatus.mutate({ id: application.id, status: previous });
      });
    },

    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: qk.applicationLists() });
    },
  });
}

/**
 * `POST /applications/{id}/retry` — **not** optimistic.
 *
 * It re-drives a real browser flow, so the local status flips to `preparing` and then waits
 * for the pipeline's own frames. Showing anything more confident would be a claim about the
 * outside world that this client is in no position to make.
 */
export function useRetryApplication() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (id: Uuid) => api.retryApplication(id),

    onMutate: async (id) => {
      await cancelFor([qk.applicationDetail(id), qk.applicationLists()]);
      queryClient.setQueryData<ApplicationDetail>(qk.applicationDetail(id), (old) =>
        old === undefined ? undefined : { ...old, status: 'preparing', last_error: null },
      );
      patchListRow<ApplicationRead>(qk.applicationLists(), id, {
        status: 'preparing',
        last_error: null,
      });
    },

    onError: (error, id) => {
      notifyError('Could not retry the application', error);
      void queryClient.invalidateQueries({ queryKey: qk.applicationDetail(id) });
      void queryClient.invalidateQueries({ queryKey: qk.applicationLists() });
    },
  });
}
