/**
 * The review queue — the highest-stakes screen in the product.
 *
 * Every entry here exists because the agent refused to guess (golden rule #2), so both
 * mutations are the moment a human takes responsibility for something the machine would not.
 * Neither is optimistic (`docs/UI.md` §10.9):
 *
 * - **Resolve** releases a real submission. It is what turns "the form asked something I
 *   could not answer" into a document arriving at an employer, and the only honest local
 *   state for it is `submitting`.
 * - **Dismiss** abandons the application. It returns the updated row, so the cache takes the
 *   server's answer rather than a predicted one.
 *
 * The list itself is the one query family in the app with a short `staleTime` (15 seconds):
 * §10.3 calls it out as the only queue where a stale count is user-visible harm, because it
 * is the number on the sidebar badge and the first thing the morning read acts on.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import * as api from '@/lib/api/endpoints';
import type {
  ApplicationDetail,
  ApplicationFilters,
  ApplicationRead,
  ReviewResolve,
  Uuid,
} from '@/lib/api/types';
import { notifyError, notifyIfNotStarted } from '@/lib/notify';
import { qk } from '@/lib/query/keys';
import { reviewListOptions } from '@/lib/query/options';
import { cancelFor, patchListRow } from '@/lib/query/optimistic';

/** `GET /reviews`. */
export function useReviews(filters: ApplicationFilters = {}) {
  return useQuery(reviewListOptions(filters));
}

/**
 * The number the sidebar badge shows.
 *
 * A `select` narrows the subscription to the total, so the badge does not re-render when a
 * row deep in the list changes. The selector is module-scope-stable because it is declared
 * once outside the hook — an inline arrow would be a new reference every render and would
 * defeat both the memo and structural sharing.
 */
const selectTotal = (page: { total: number }): number => page.total;

/** The review-queue count, for the sidebar badge and the page-header subtitle. */
export function useReviewCount(): number {
  const { data } = useQuery({ ...reviewListOptions({}), select: selectTotal });
  return data ?? 0;
}

/**
 * `POST /reviews/{id}/resolve` — **not optimistic**.
 *
 * The local status moves to `submitting`, which is true the instant the server accepts: the
 * pipeline has resumed. Everything after that arrives as a real `application.*` frame.
 */
export function useResolveReview() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({ id, body }: { id: Uuid; body: ReviewResolve }) =>
      api.resolveReview(id, body),

    onMutate: async ({ id }) => {
      await cancelFor([qk.applicationDetail(id), qk.applicationLists(), qk.reviews()]);
      queryClient.setQueryData<ApplicationDetail>(qk.applicationDetail(id), (old) =>
        old === undefined ? undefined : { ...old, status: 'submitting', needs_review: false },
      );
      patchListRow<ApplicationRead>(qk.applicationLists(), id, {
        status: 'submitting',
        needs_review: false,
      });
    },

    onError: (error, { id }) => {
      notifyError('Could not submit this review', error);
      void queryClient.invalidateQueries({ queryKey: qk.applicationDetail(id) });
      void queryClient.invalidateQueries({ queryKey: qk.reviews() });
    },

    onSuccess: (response) => {
      // The optimistic write above moved the row to `submitting`. If nothing is running
      // `apply.submit`, that status is a claim the system cannot keep — and this is the one
      // place in the product where a stuck row means "the employer never heard from you".
      notifyIfNotStarted(response, 'The submission');
    },

    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: qk.reviews(), refetchType: 'active' });
    },
  });
}

/**
 * `POST /reviews/{id}/dismiss` — abandon rather than answer.
 *
 * Destructive (`Ctrl+X`, always confirms). The response is the updated application, so it is
 * written straight into the cache and the queue is invalidated to drop the entry.
 */
export function useDismissReview() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({ id, note }: { id: Uuid; note?: string }) =>
      api.dismissReview(id, note === undefined ? {} : { note }),

    onSuccess: (application) => {
      queryClient.setQueryData<ApplicationDetail>(
        qk.applicationDetail(application.id),
        (old) => (old === undefined ? undefined : { ...old, ...application }),
      );
      patchListRow<ApplicationRead>(qk.applicationLists(), application.id, application);
      void queryClient.invalidateQueries({ queryKey: qk.reviews(), refetchType: 'active' });
    },

    onError: (error) => {
      notifyError('Could not dismiss this review', error);
    },
  });
}
