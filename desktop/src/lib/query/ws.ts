/**
 * The live event stream: `GET /ws` → `queryClient.setQueryData` (`docs/UI.md` §10.8).
 *
 * `app/api/events.py` publishes payloads that are *the same pydantic schemas the REST
 * endpoints return*, explicitly so the desktop client can write them straight into the cache
 * without refetching. That is the contract, and this module is the only consumer of it.
 *
 * **Four binding rules, all implemented below.**
 *
 * 1. **`setQueryData`, never `invalidateQueries`, whenever the frame carries the entity.**
 *    `setQueryData` writes data and bumps `dataUpdatedAt`; it is *structurally incapable* of
 *    producing a pending state, so a live update can never flash a spinner.
 *    `invalidateQueries` marks stale and refetches, so it is used only for frames that carry
 *    counters rather than entities — and always with `refetchType: 'active'`, so background
 *    queries do not stampede.
 * 2. **Preserve referential identity for untouched rows.** `items.map(i => i.id === x.id ?
 *    {...i, ...x} : i)` — never a blanket `{...i}` spread, which defeats `structuralSharing`
 *    and re-renders every memoised row on every event. That one line decides 60fps vs 12fps
 *    on a table during a run.
 * 3. **Batch and coalesce.** Frames are buffered in a module-level array and flushed on
 *    `requestAnimationFrame`, so 500 events/s becomes at most 60 cache writes/s. Each flush
 *    runs inside `notifyManager.batch()`, so N key writes cost one render pass.
 * 4. **Log lines never touch the cache.** They go to the ring buffer in `lib/log-buffer.ts`.
 *
 * **Recorded deviation.** `docs/UI.md` §10.8 and §13 say to host the socket in the Rust
 * shell and relay frames to the renderer, so it survives reloads and background throttling.
 * The shipped shell (`src-tauri/src/`) exposes six commands and no event relay, and the shell
 * is out of this module's scope. The socket therefore lives in the renderer, behind the
 * {@link EventTransport} seam: when a `backend://event` relay is added, only
 * {@link connectEvents} changes and nothing that consumes {@link subscribeConnection} does.
 */

import { notifyManager } from '@tanstack/react-query';

import type {
  AnalyticsOverview,
  ApplicationDetail,
  ApplicationRead,
  AppEvent,
  PostingRead,
  ScoreRead,
  SessionDetail,
  SessionRead,
} from '@/lib/api/types';
import { pushLog } from '@/lib/log-buffer';
import { toWebSocketOrigin, resolveBaseUrl } from '@/lib/tauri';
import { useSessionStore } from '@/stores/session';

import { queryClient } from './client';
import { qk } from './keys';
import { patchListRow } from './optimistic';

/** Connection state, as the status bar renders it. */
export type ConnectionState = 'idle' | 'connecting' | 'open' | 'reconnecting' | 'closed';

/** Backoff floor, in milliseconds. Short enough that a sidecar restart is invisible. */
const BACKOFF_MIN_MS = 250;

/** Backoff ceiling. Past this the backend is not coming back on its own. */
const BACKOFF_MAX_MS = 10_000;

/**
 * Frames buffered before the oldest is dropped inside one animation frame.
 *
 * The server's own mailbox is 512 frames deep; this is the client-side twin, and it exists
 * so that a burst which arrives faster than the compositor can never grow without bound.
 */
const MAX_PENDING_FRAMES = 2_000;

let socket: WebSocket | null = null;
let state: ConnectionState = 'idle';
let attempt = 0;
let reconnectTimer: number | null = null;
let rafHandle: number | null = null;
let stopped = true;
let pending: AppEvent[] = [];

const stateListeners = new Set<() => void>();

// ══════════════════════════════════════════════════════════════════════════════════════
// Connection state, exposed for `useSyncExternalStore`
// ══════════════════════════════════════════════════════════════════════════════════════

function setState(next: ConnectionState): void {
  if (state === next) return;
  state = next;
  for (const listener of stateListeners) listener();
}

/** Subscribe to connection-state changes. */
export function subscribeConnection(listener: () => void): () => void {
  stateListeners.add(listener);
  return () => {
    stateListeners.delete(listener);
  };
}

/** The current connection state. Stable between transitions. */
export function getConnectionState(): ConnectionState {
  return state;
}

// ══════════════════════════════════════════════════════════════════════════════════════
// Cache writers
// ══════════════════════════════════════════════════════════════════════════════════════

/**
 * Rule 2 — preserve referential identity for untouched rows — lives in
 * {@link patchListRow}, shared with the optimistic-mutation path so the socket and a local
 * edit write the cache exactly the same way.
 */

/** Merge an application into its detail entry and every cached list page. */
function applyApplication(application: ApplicationRead): void {
  queryClient.setQueryData<ApplicationDetail>(
    qk.applicationDetail(application.id),
    (old) => (old === undefined ? undefined : { ...old, ...application }),
  );
  patchListRow<ApplicationRead>(qk.applicationLists(), application.id, application);
}

/** Merge a session into its detail entry and every cached list page. */
function applySession(session: SessionRead): void {
  queryClient.setQueryData<SessionDetail>(qk.sessionDetail(session.id), (old) =>
    old === undefined ? undefined : { ...old, ...session },
  );
  patchListRow<SessionRead>([...qk.sessions(), 'list'], session.id, session);
}

/**
 * Attach a freshly computed score to its posting, everywhere it is cached.
 *
 * `posting.scored` carries a `ScoreRead`, not a posting, so this is a nested patch rather
 * than a merge — and it is still strictly better than invalidating, because the score is the
 * only thing that changed and the posting row keeps its identity.
 */
function applyScore(score: ScoreRead): void {
  queryClient.setQueryData<PostingRead>(qk.postingDetail(score.posting_id), (old) =>
    old === undefined ? undefined : { ...old, score, status: 'scored' },
  );
  patchListRow<PostingRead>([...qk.postings(), 'list'], score.posting_id, {
    score,
    status: 'scored',
  });
}

/**
 * Nudge the analytics figures without refetching them.
 *
 * Only the counters the frame is unambiguous about are touched. Anything derived — response
 * rate, funnel conversion, the timeseries — is left to the next real fetch, because a
 * client-side recomputation that drifts from the server's arithmetic is worse than a figure
 * that is thirty seconds old.
 */
function bumpDashboard(mutate: (stats: AnalyticsOverview['stats']) => AnalyticsOverview['stats']): void {
  queryClient.setQueriesData<AnalyticsOverview>(
    { queryKey: [...qk.analyticsAll(), 'overview'] },
    (old) => (old === undefined ? old : { ...old, stats: mutate(old.stats) }),
  );
}

/** Apply one frame. Runs inside a `notifyManager.batch`, so every write here is one render. */
function handle(frame: AppEvent): void {
  switch (frame.event) {
    case 'application.created':
    case 'application.status_changed':
    case 'application.submitted':
    case 'application.needs_review': {
      const application = frame.payload;
      applyApplication(application);

      // The review queue's entries are `ReviewItem`s, not applications — the frame does not
      // carry the unanswered fields — so this is one of the few honest invalidations.
      if (application.needs_review || frame.event === 'application.needs_review') {
        void queryClient.invalidateQueries({
          queryKey: qk.reviews(),
          refetchType: 'active',
        });
      }
      if (frame.event === 'application.submitted') {
        bumpDashboard((stats) => ({
          ...stats,
          applications_today: stats.applications_today + 1,
        }));
        void queryClient.invalidateQueries({
          queryKey: qk.applicationArtifacts(application.id),
          refetchType: 'active',
        });
      }
      break;
    }

    case 'session.started':
    case 'session.updated':
    case 'session.finished': {
      applySession(frame.payload);
      // The status bar's "● running · 12 applied" reads the store, not the cache: it is a
      // transient, and a run that has finished must stop being one on the same frame.
      useSessionStore.getState().applySession(frame.payload);
      if (frame.event === 'session.finished') {
        void queryClient.invalidateQueries({
          queryKey: qk.analyticsAll(),
          refetchType: 'active',
        });
      }
      break;
    }

    case 'posting.scored':
      applyScore(frame.payload);
      break;

    case 'posting.discovered':
      // A run-level counter frame, not an entity: there is nothing to write, so the posting
      // lists are marked stale and the visible one refetches behind the IndeterminateBar.
      void queryClient.invalidateQueries({
        queryKey: qk.postings(),
        refetchType: 'active',
      });
      break;

    // Progress frames drive the ProgressRing through the store, never the cache: a
    // percentage is not an entity, and writing one into the cache would make it persistable.
    case 'knowledge.index_started':
      useSessionStore.getState().startIndexing(frame.payload.source_id ?? null);
      break;

    case 'knowledge.index_progress': {
      const { progress, documents, chunks } = frame.payload;
      useSessionStore.getState().updateIndexing({
        progress: progress ?? null,
        ...(documents === undefined ? {} : { documents }),
        ...(chunks === undefined ? {} : { chunks }),
      });
      break;
    }

    case 'knowledge.index_finished':
      useSessionStore.getState().finishIndexing();
      void queryClient.invalidateQueries({
        queryKey: qk.knowledge(),
        refetchType: 'active',
      });
      break;

    case 'tracking.status_changed': {
      const { application_id: id, status } = frame.payload;
      queryClient.setQueryData<ApplicationDetail>(qk.applicationDetail(id), (old) =>
        old === undefined ? undefined : { ...old, status },
      );
      patchListRow<ApplicationRead>(qk.applicationLists(), id, { status });
      void queryClient.invalidateQueries({
        queryKey: qk.trackingStats(),
        refetchType: 'active',
      });
      break;
    }

    case 'tracking.sync_started':
      useSessionStore.getState().startSync(frame.payload.account_id);
      break;

    case 'tracking.sync_progress':
      useSessionStore.getState().updateSync({
        fetched: frame.payload.fetched,
        classified: frame.payload.classified,
        matched: frame.payload.matched,
        applied: frame.payload.applied,
        needsReview: frame.payload.needs_review,
      });
      break;

    case 'tracking.sync_finished':
      useSessionStore.getState().finishSync();
      void queryClient.invalidateQueries({
        queryKey: qk.tracking(),
        refetchType: 'active',
      });
      break;

    case 'tracking.signal_found':
      void queryClient.invalidateQueries({
        queryKey: qk.tracking(),
        refetchType: 'active',
      });
      break;

    case 'log.entry':
      // Never the query cache. See `lib/log-buffer.ts`.
      pushLog(frame.payload);
      break;

    default: {
      // A frame from a newer backend. Nothing is assumed about it, and nothing is
      // invalidated on its behalf — guessing here would refetch the whole app on an unknown
      // string. `frame` is `never` when the union is exhaustive, which is the point.
      const unknown: never = frame;
      void unknown;
      break;
    }
  }
}

function flush(): void {
  rafHandle = null;
  if (pending.length === 0) return;
  const frames = pending;
  pending = [];
  notifyManager.batch(() => {
    for (const frame of frames) handle(frame);
  });
}

function enqueue(frame: AppEvent): void {
  // Log lines are already coalesced by the ring buffer's own rAF, and routing them through
  // this queue would make one busy run evict every other frame from it.
  if (frame.event === 'log.entry') {
    pushLog(frame.payload);
    return;
  }
  if (pending.length >= MAX_PENDING_FRAMES) pending.shift();
  pending.push(frame);
  if (rafHandle === null) rafHandle = window.requestAnimationFrame(flush);
}

// ══════════════════════════════════════════════════════════════════════════════════════
// Transport
// ══════════════════════════════════════════════════════════════════════════════════════

/** The seam a shell-hosted relay would replace. */
export interface EventTransport {
  /** Stop the stream and release the connection. */
  close: () => void;
}

function backoffDelay(): number {
  const base = Math.min(BACKOFF_MIN_MS * 2 ** attempt, BACKOFF_MAX_MS);
  // Full jitter. Without it, a sidecar restart reconnects every window in lockstep.
  return base / 2 + Math.random() * (base / 2);
}

function scheduleReconnect(): void {
  if (stopped || reconnectTimer !== null) return;
  setState('reconnecting');
  const delay = backoffDelay();
  attempt += 1;
  reconnectTimer = window.setTimeout(() => {
    reconnectTimer = null;
    void open();
  }, delay);
}

async function open(): Promise<void> {
  if (stopped || socket !== null) return;
  setState(attempt === 0 ? 'connecting' : 'reconnecting');

  let url: string;
  try {
    url = `${toWebSocketOrigin(await resolveBaseUrl())}/ws`;
  } catch {
    // The backend has not reported a port yet. That is a normal cold-start state, not an
    // error, so it is retried on the same backoff as a dropped socket.
    scheduleReconnect();
    return;
  }
  if (stopped) return;

  const ws = new WebSocket(url);
  socket = ws;

  ws.onopen = () => {
    attempt = 0;
    setState('open');
    // Everything the socket covers has `staleTime: Infinity`, so a reconnect is the one
    // moment the client knows it may have missed frames. Refetching only what is mounted
    // repairs the visible screen without a stampede across the whole cache.
    void queryClient.invalidateQueries({ refetchType: 'active' });
  };

  ws.onmessage = (message: MessageEvent<string>) => {
    try {
      enqueue(JSON.parse(message.data) as AppEvent);
    } catch {
      // A frame this build cannot parse. Dropping it is correct: the alternative is
      // tearing down a healthy socket over one malformed message.
    }
  };

  ws.onerror = () => {
    // `onclose` always follows, and it is the one that owns the retry.
  };

  ws.onclose = () => {
    if (socket === ws) socket = null;
    if (stopped) {
      setState('closed');
      return;
    }
    scheduleReconnect();
  };
}

/**
 * Start the event stream.
 *
 * Call once, at module scope, outside React — a subscriber created inside a component would
 * be torn down and rebuilt by every Fast Refresh and by StrictMode's double-mount, and each
 * cycle would cost a real reconnect.
 *
 * @returns A handle that closes the stream. Idempotent.
 */
export function connectEvents(): EventTransport {
  stopped = false;
  void open();
  return {
    close: () => {
      stopped = true;
      if (reconnectTimer !== null) {
        window.clearTimeout(reconnectTimer);
        reconnectTimer = null;
      }
      if (rafHandle !== null) {
        window.cancelAnimationFrame(rafHandle);
        rafHandle = null;
      }
      pending = [];
      attempt = 0;
      socket?.close();
      socket = null;
      setState('closed');
    },
  };
}

/**
 * Force a reconnect now, skipping the backoff.
 *
 * Used after `restart_backend()`: the port changed, so the current socket is pointed at a
 * closed one and waiting out the backoff would leave the app blind for up to ten seconds.
 */
export function reconnectEvents(): void {
  if (reconnectTimer !== null) {
    window.clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
  attempt = 0;
  const current = socket;
  socket = null;
  current?.close();
  if (!stopped) void open();
}

/**
 * Apply one frame directly, bypassing the socket.
 *
 * The seam a shell-hosted relay would call. Also what lets the status-sync "run now" flow
 * feed a locally constructed frame through exactly the same code path as a real one.
 */
export function dispatchEvent(frame: AppEvent): void {
  enqueue(frame);
}
