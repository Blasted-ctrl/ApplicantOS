/**
 * React's view of the live log tail.
 *
 * `useSyncExternalStore` rather than state-in-an-effect, because the buffer is written from
 * outside React (the WebSocket handler, at up to frame rate) and this is precisely the
 * problem that hook exists for: it guarantees every component reading the tail sees the same
 * snapshot in the same commit, with no tearing on a fast stream.
 *
 * The connection between the reader's scroll position and the flush cadence is deliberate
 * (`docs/UI.md` §7.21 rule 3): while the reader is scrolled away from the bottom, nothing
 * they are looking at is changing, so the buffer drops to a 250ms cadence and the virtualiser
 * stops re-rendering sixty times a second for no visible benefit.
 */

import { useCallback, useSyncExternalStore } from 'react';

import {
  clearLogs,
  getLogsServerSnapshot,
  getLogsSnapshot,
  receivedCount,
  setFollowing,
  subscribeLogs,
  type LogLine,
} from '@/lib/log-buffer';

/** The tail, as a stable array reference between flushes. */
export function useLogStream(): readonly LogLine[] {
  return useSyncExternalStore(subscribeLogs, getLogsSnapshot, getLogsServerSnapshot);
}

/** Controls for the LogStream toolbar. */
export interface LogStreamControls {
  /** Empty the ring. */
  clear: () => void;
  /**
   * Report whether the viewport is pinned within 50px of the bottom.
   *
   * The threshold is what distinguishes a deliberate scroll-up from layout jitter, so it
   * belongs to the component that owns the scroll container, not to this hook.
   */
  setFollowing: (following: boolean) => void;
  /** Total lines accepted since launch, including ones the ring has since dropped. */
  received: () => number;
}

/** The imperative half of the log tail. Stable across renders. */
export function useLogStreamControls(): LogStreamControls {
  const clear = useCallback(() => {
    clearLogs();
  }, []);
  const follow = useCallback((following: boolean) => {
    setFollowing(following);
  }, []);
  const received = useCallback(() => receivedCount(), []);
  return { clear, setFollowing: follow, received };
}
