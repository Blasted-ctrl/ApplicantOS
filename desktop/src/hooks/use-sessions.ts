/**
 * Automation runs: the list, one run's detail, and the two controls that start and stop one.
 *
 * Neither control is optimistic (`docs/UI.md` §10.9). Starting a run spins up browser
 * automation; stopping one interrupts it mid-flight. Both are answered by the server and both
 * announce themselves as `session.*` frames, which is what actually updates the status bar —
 * the mutation's own response is only the acknowledgement that the request was accepted.
 *
 * `Ctrl+S` is the stop binding, in the `Ctrl` namespace that means irreversible (§9.1), and
 * §9.2 requires it to confirm first. That confirmation is the caller's dialog, not this
 * hook's job — but the binding is why {@link useStopSession} takes an explicit id rather than
 * inferring "the running one": a confirmation the user answers about a specific run must act
 * on that run.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import * as api from '@/lib/api/endpoints';
import type { PageParams, SessionRead, SessionStartRequest, Uuid } from '@/lib/api/types';
import { notifyError } from '@/lib/notify';
import { qk } from '@/lib/query/keys';
import { sessionDetailOptions, sessionListOptions } from '@/lib/query/options';
import { useSessionStore } from '@/stores/session';

/** `GET /sessions`. */
export function useSessions(params: PageParams = {}) {
  return useQuery(sessionListOptions(params));
}

/** `GET /sessions/{id}`. */
export function useSession(id: Uuid | undefined) {
  return useQuery({
    ...sessionDetailOptions(id ?? ''),
    enabled: id !== undefined && id !== '',
  });
}

/** The run currently executing, from the live store rather than from a query. */
export function useActiveSession(): SessionRead | null {
  return useSessionStore((s) => s.activeSession);
}

/**
 * `POST /sessions/start` — **not optimistic**.
 *
 * The `session.started` frame is what puts the run in the store and lights the status bar.
 * Anything this hook wrote locally would be a duplicate that had to be reconciled a moment
 * later, and would be wrong if the server refused the request.
 */
export function useStartSession() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (body: SessionStartRequest = {}) => api.startSession(body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: qk.sessions(), refetchType: 'active' });
    },
    onError: (error) => {
      notifyError('Could not start the run', error);
    },
  });
}

/**
 * `POST /sessions/{id}/stop` — destructive, and the caller must confirm first.
 *
 * Returns the stopped session, which is written straight into the cache: the server's row is
 * authoritative about *why* it stopped (`cancelled` versus `failed`), and that distinction is
 * exactly what the run panel shows.
 */
export function useStopSession() {
  const queryClient = useQueryClient();
  const applySession = useSessionStore((s) => s.applySession);

  return useMutation({
    mutationFn: (id: Uuid) => api.stopSession(id),
    onSuccess: (session) => {
      queryClient.setQueryData(qk.sessionDetail(session.id), (old) =>
        old === undefined ? undefined : { ...old, ...session },
      );
      applySession(session);
      void queryClient.invalidateQueries({ queryKey: qk.sessions(), refetchType: 'active' });
    },
    onError: (error) => {
      notifyError('Could not stop the run', error);
    },
  });
}
