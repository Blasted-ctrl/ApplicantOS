/**
 * Application status sync (`docs/CONTRACTS.md` §17) — connected mailboxes and the signals
 * they produce.
 *
 * This is the subsystem that closes the loop: a rejection finds its own way into the database
 * whether or not the user ever opens the app. The UI's job is to make the privacy posture
 * legible, because §17.8 makes it non-negotiable and invisible guarantees are worth nothing:
 *
 * - **Read-only, always.** Nothing here can send, delete, move, or re-flag a message.
 * - **Credentials never touch the database.** `EmailAccountCreate.secret` is forwarded to the
 *   OS keychain and is never echoed back, which is why an account form must treat an empty
 *   secret as "leave unchanged" rather than as "clear".
 * - **Minimal retention.** A `StatusSignalRead` carries a subject and a snippet capped at 500
 *   characters. There is no full message body to render, and the UI must not imply there is.
 *
 * Resolving a signal is not optimistic: it moves an application's status on the strength of a
 * human's reading of an email, and the server returns the updated application.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import * as api from '@/lib/api/endpoints';
import type {
  ApplicationDetail,
  ApplicationRead,
  EmailAccountCreate,
  SignalFilters,
  SignalResolve,
  SyncRequest,
  Uuid,
} from '@/lib/api/types';
import { notifyError, notifyIfNotStarted } from '@/lib/notify';
import { qk } from '@/lib/query/keys';
import {
  emailAccountsOptions,
  signalsOptions,
  trackingStatsOptions,
} from '@/lib/query/options';
import { patchListRow } from '@/lib/query/optimistic';

/** `GET /tracking/accounts`. */
export function useEmailAccounts() {
  return useQuery(emailAccountsOptions());
}

/** `GET /tracking/signals`. */
export function useSignals(filters: SignalFilters = {}) {
  return useQuery(signalsOptions(filters));
}

/** `GET /tracking/stats`. */
export function useTrackingStats() {
  return useQuery(trackingStatsOptions());
}

/** `POST /tracking/accounts` — connect one mailbox. The secret goes to the OS keychain. */
export function useCreateEmailAccount() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (body: EmailAccountCreate) => api.createEmailAccount(body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: qk.tracking(), refetchType: 'active' });
    },
    onError: (error) => {
      notifyError('Could not connect the mailbox', error);
    },
  });
}

/**
 * `DELETE /tracking/accounts/{id}`.
 *
 * Disconnecting deletes the stored credential reference and the sync cursor, so it is
 * destructive and the caller confirms first.
 */
export function useDeleteEmailAccount() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (id: Uuid) => api.deleteEmailAccount(id),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: qk.tracking(), refetchType: 'active' });
    },
    onError: (error) => {
      notifyError('Could not disconnect the mailbox', error);
    },
  });
}

/** `POST /tracking/accounts/{id}/test` — a read-only connection probe. */
export function useTestEmailAccount() {
  return useMutation({
    mutationFn: (id: Uuid) => api.testEmailAccount(id),
    onError: (error) => {
      notifyError('Could not reach the mailbox', error);
    },
  });
}

/**
 * `POST /tracking/sync` — run a sync now.
 *
 * Answered 202. Progress arrives as `tracking.sync_*` frames and drives the store; there is
 * nothing to write into the cache here.
 */
export function useRunSync() {
  return useMutation({
    mutationFn: (body: SyncRequest = {}) => api.runSync(body),
    onSuccess: (response) => {
      notifyIfNotStarted(response, 'The mailbox sync');
    },
    onError: (error) => {
      notifyError('Could not start the sync', error);
    },
  });
}

/**
 * `POST /tracking/signals/{id}/resolve` — bind a signal to an application and apply it.
 *
 * The one status change in the product that a human authorises on the strength of an email.
 * The server returns the updated application and that answer is written directly, because
 * `status_source` and `status_confidence` are derived server-side and a predicted row would
 * claim the wrong provenance.
 */
export function useResolveSignal() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({ id, body }: { id: Uuid; body: SignalResolve }) =>
      api.resolveSignal(id, body),

    onSuccess: (application) => {
      queryClient.setQueryData<ApplicationDetail>(
        qk.applicationDetail(application.id),
        (old) => (old === undefined ? undefined : { ...old, ...application }),
      );
      patchListRow<ApplicationRead>(qk.applicationLists(), application.id, application);
      void queryClient.invalidateQueries({ queryKey: qk.tracking(), refetchType: 'active' });
    },

    onError: (error) => {
      notifyError('Could not apply this signal', error);
    },
  });
}

/** `POST /tracking/signals/{id}/dismiss`. */
export function useDismissSignal() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (id: Uuid) => api.dismissSignal(id),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: qk.tracking(), refetchType: 'active' });
    },
    onError: (error) => {
      notifyError('Could not dismiss the signal', error);
    },
  });
}
