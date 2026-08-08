/**
 * The postings family: list, detail, discovery, and "apply now".
 *
 * **`POST /postings/{id}/apply` is never optimistic** (`docs/UI.md` §10.9). It is the call
 * that can put a real document in front of a real employer, and `docs/CONTRACTS.md` §19.1
 * guards it server-side with both a unique constraint and a status check. The UI's job is to
 * flip the posting to `processing` and wait for the pipeline's own `application.*` frames —
 * showing "Applied" and rolling back would be a lie about something irreversible.
 *
 * The apply affordance is additionally gated on {@link AUTO_APPLY_PROVIDERS}: LinkedIn's ToS
 * prohibits automated submission and Workday's account-gated flow routes to review by design,
 * so offering the button for those two would promise something the backend will refuse.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import * as api from '@/lib/api/endpoints';
import {
  AUTO_APPLY_PROVIDERS,
  type DiscoverRequest,
  type PostingFilters,
  type PostingRead,
  type Uuid,
} from '@/lib/api/types';
import { notifyError } from '@/lib/notify';
import { qk } from '@/lib/query/keys';
import { postingDetailOptions, postingListOptions } from '@/lib/query/options';
import { cancelFor, patchListRow } from '@/lib/query/optimistic';

/** `GET /postings`. */
export function usePostings(filters: PostingFilters = {}) {
  return useQuery(postingListOptions(filters));
}

/** `GET /postings/{id}`. */
export function usePosting(id: Uuid | undefined) {
  return useQuery({
    ...postingDetailOptions(id ?? ''),
    enabled: id !== undefined && id !== '',
  });
}

/**
 * Whether this posting can be applied to automatically.
 *
 * Two independent conditions, both from the contract: the provider must support submission,
 * and the posting must still be open and not already actioned.
 */
export function canAutoApply(posting: PostingRead): boolean {
  return (
    AUTO_APPLY_PROVIDERS.has(posting.provider) &&
    posting.is_open &&
    posting.status !== 'applied' &&
    posting.status !== 'processing' &&
    posting.status !== 'expired'
  );
}

/**
 * `POST /postings/discover` — start a discovery run.
 *
 * Answered 202: the work happens on the `discovery` queue and reports back as
 * `posting.discovered` frames, so there is nothing to write into the cache here.
 */
export function useDiscoverPostings() {
  return useMutation({
    mutationFn: (body: DiscoverRequest = {}) => api.discoverPostings(body),
    onError: (error) => {
      notifyError('Could not start discovery', error);
    },
  });
}

/**
 * `POST /postings/{id}/apply` — queue one posting. **Not optimistic.**
 *
 * The local status moves to `processing`, which is a statement about the *pipeline*, not
 * about the employer. Every further transition comes from a real event.
 */
export function useApplyToPosting() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (id: Uuid) => api.applyToPosting(id),

    onMutate: async (id) => {
      await cancelFor([qk.postingDetail(id), qk.postings()]);
      queryClient.setQueryData<PostingRead>(qk.postingDetail(id), (old) =>
        old === undefined ? undefined : { ...old, status: 'processing' },
      );
      patchListRow<PostingRead>([...qk.postings(), 'list'], id, { status: 'processing' });
    },

    onError: (error, id) => {
      notifyError('Could not queue this posting', error);
      // The local `processing` was a guess about a request that failed; the server's answer
      // is the only one worth showing.
      void queryClient.invalidateQueries({ queryKey: qk.postingDetail(id) });
      void queryClient.invalidateQueries({ queryKey: qk.postings() });
    },

    onSuccess: () => {
      // The application row is created server-side and arrives as `application.created`;
      // the list is invalidated so a user already on `/applications` sees it appear.
      void queryClient.invalidateQueries({
        queryKey: qk.applicationLists(),
        refetchType: 'active',
      });
    },
  });
}
