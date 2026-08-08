/**
 * Live runtime state: the running automation run, indexing progress, mailbox sync, and the
 * backend process itself.
 *
 * Everything here arrives over the WebSocket or from the Tauri shell, and none of it is a
 * *resource* — there is no `GET /indexing-progress` and there never will be, because progress
 * is a stream, not a document. That is the line between this store and TanStack Query: an
 * entity the server can be asked for lives in the cache; a transient that only exists while
 * something is happening lives here.
 *
 * Practically, this is what the status bar reads (`docs/UI.md` §5.1), what the ProgressRing
 * binds to (§7.22), and what decides whether `Ctrl+S` has a session to stop.
 */

import { create } from 'zustand';

import type { SessionRead, Uuid } from '@/lib/api/types';
import type { BackendStatus } from '@/lib/tauri';

/** Indexing progress for one scope — a single source, or a whole-user reindex. */
export interface IndexingProgress {
  /** The source being indexed, when the run is scoped to one. */
  sourceId: Uuid | null;
  /** 0–1 when the total is genuinely known. `null` means use an IndeterminateBar, not a ring. */
  progress: number | null;
  documents: number;
  chunks: number;
  startedAt: number;
}

/** Mailbox sync progress, from the `tracking.sync_*` frames. */
export interface SyncProgress {
  accountId: Uuid | null;
  fetched: number;
  classified: number;
  matched: number;
  applied: number;
  needsReview: number;
  startedAt: number;
}

interface SessionState {
  /** The run currently executing, if any. Cleared when it reaches a finished status. */
  activeSession: SessionRead | null;
  /** Indexing progress, or `null` when nothing is indexing. */
  indexing: IndexingProgress | null;
  /** Mailbox sync progress, or `null` when no sync is running. */
  syncing: SyncProgress | null;
  /** Backend process status, pushed by the Tauri shell on `backend://status`. */
  backend: BackendStatus | null;

  applySession: (session: SessionRead) => void;
  clearSession: () => void;
  startIndexing: (sourceId: Uuid | null) => void;
  updateIndexing: (patch: Partial<Omit<IndexingProgress, 'startedAt'>>) => void;
  finishIndexing: () => void;
  startSync: (accountId: Uuid | null) => void;
  updateSync: (patch: Partial<Omit<SyncProgress, 'startedAt'>>) => void;
  finishSync: () => void;
  setBackend: (status: BackendStatus | null) => void;
}

/** Statuses that mean the run has stopped, however it stopped. */
const FINISHED = new Set(['completed', 'failed', 'cancelled']);

/** Live runtime state. Nothing here is persisted: none of it survives the process it describes. */
export const useSessionStore = create<SessionState>()((set) => ({
  activeSession: null,
  indexing: null,
  syncing: null,
  backend: null,

  applySession: (session) => {
    set({ activeSession: FINISHED.has(session.status) ? null : session });
  },
  clearSession: () => {
    set({ activeSession: null });
  },

  startIndexing: (sourceId) => {
    set({
      indexing: {
        sourceId,
        progress: null,
        documents: 0,
        chunks: 0,
        startedAt: Date.now(),
      },
    });
  },
  updateIndexing: (patch) => {
    set((s) =>
      s.indexing === null ? s : { indexing: { ...s.indexing, ...patch } },
    );
  },
  finishIndexing: () => {
    set({ indexing: null });
  },

  startSync: (accountId) => {
    set({
      syncing: {
        accountId,
        fetched: 0,
        classified: 0,
        matched: 0,
        applied: 0,
        needsReview: 0,
        startedAt: Date.now(),
      },
    });
  },
  updateSync: (patch) => {
    set((s) => (s.syncing === null ? s : { syncing: { ...s.syncing, ...patch } }));
  },
  finishSync: () => {
    set({ syncing: null });
  },

  setBackend: (backend) => {
    set({ backend });
  },
}));

/** Whether a run is currently executing. What `Ctrl+S` and the status bar's dot consult. */
export function useIsRunning(): boolean {
  return useSessionStore((s) => s.activeSession !== null);
}

/** Whether the backend is serving. `false` while it starts, and after it fails. */
export function useBackendReady(): boolean {
  return useSessionStore((s) => s.backend === null || s.backend.phase === 'ready');
}
