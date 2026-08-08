/**
 * The live log tail (`docs/UI.md` §7.21) — a module-level ring buffer, deliberately **not** a
 * query.
 *
 * This is the one place in the app where server data does not live in the query cache, and
 * the reason is mechanical: an append-per-line cache entry churns the cache on every line and
 * triggers the persister on every write. A busy run emits hundreds of lines a second; routed
 * through TanStack Query that is hundreds of cache mutations, hundreds of IndexedDB writes,
 * and a re-render of every observer, per second.
 *
 * Three properties make the LogStream the most performance-sensitive component in the product
 * survivable, and all three live here rather than in the component:
 *
 * 1. **A bounded ring.** 10 000 lines, never persisted. Older lines are dropped, because a
 *    log tail is a tail.
 * 2. **rAF-coalesced flush.** Every line that arrives within one frame becomes one state
 *    update. When the reader has scrolled away from the bottom the cadence drops to 250ms,
 *    because nothing they are looking at is changing.
 * 3. **A stable snapshot.** `getSnapshot` returns the same array reference until a flush
 *    actually happens, which is what `useSyncExternalStore` requires and what stops React
 *    from tearing on a fast stream.
 *
 * `GET /logs` remains a normal paginated query for historical search — see
 * `lib/api/endpoints.ts`. The two never mix.
 */

import type { JsonObject, LogEntryRead } from '@/lib/api/types';

/** Hard cap on retained lines. */
export const LOG_BUFFER_CAPACITY = 10_000;

/** Flush cadence, in milliseconds, while the reader is scrolled away from the bottom. */
const DETACHED_FLUSH_MS = 250;

/** One rendered log line. */
export interface LogLine {
  /**
   * Client-assigned monotonic sequence.
   *
   * The virtualiser's `getItemKey`. Deliberately not the server id: a line that arrives over
   * the socket before it has been persisted has no id yet, and an index-based key would make
   * React reuse the wrong DOM the moment the ring drops its head.
   */
  seq: number;
  at: string;
  level: string;
  event: string;
  logger: string | null;
  correlation_id: string | null;
  payload: JsonObject;
}

/** Convert a persisted log row into a renderable line. */
export function toLogLine(entry: LogEntryRead, seq: number): LogLine {
  return {
    seq,
    at: entry.at,
    level: entry.level,
    event: entry.event,
    logger: entry.logger ?? null,
    correlation_id: entry.correlation_id ?? null,
    payload: entry.payload,
  };
}

type Listener = () => void;

/** Lines already visible to React. Replaced wholesale on flush; never mutated in place. */
let snapshot: readonly LogLine[] = [];

/** Lines accepted since the last flush. */
let pending: LogLine[] = [];

/** The retained ring. Kept separate from {@link snapshot} so a flush is one array copy. */
let ring: LogLine[] = [];

const listeners = new Set<Listener>();

let nextSeq = 1;
let rafHandle: number | null = null;
let timerHandle: number | null = null;
let following = true;
let totalReceived = 0;

/** Total lines accepted since launch, including ones the ring has since dropped. */
export function receivedCount(): number {
  return totalReceived;
}

function emit(): void {
  for (const listener of listeners) listener();
}

function flush(): void {
  rafHandle = null;
  timerHandle = null;
  if (pending.length === 0) return;

  ring = ring.length + pending.length <= LOG_BUFFER_CAPACITY
    ? ring.concat(pending)
    : ring.concat(pending).slice(-LOG_BUFFER_CAPACITY);
  pending = [];
  snapshot = ring;
  emit();
}

function schedule(): void {
  if (following) {
    if (rafHandle !== null) return;
    // A pending timer from a detached period is superseded: the reader came back to the
    // bottom, and they should see the next line on the next frame, not 250ms later.
    if (timerHandle !== null) {
      window.clearTimeout(timerHandle);
      timerHandle = null;
    }
    rafHandle = window.requestAnimationFrame(flush);
    return;
  }
  if (timerHandle !== null) return;
  timerHandle = window.setTimeout(flush, DETACHED_FLUSH_MS);
}

/** Append one line. Cheap enough to call from the socket handler on every frame. */
export function pushLog(entry: LogEntryRead): void {
  totalReceived += 1;
  pending.push(toLogLine(entry, nextSeq));
  nextSeq += 1;
  schedule();
}

/** Append many lines as one unit — used when seeding the tail from a historical page. */
export function pushLogs(entries: readonly LogEntryRead[]): void {
  for (const entry of entries) {
    totalReceived += 1;
    pending.push(toLogLine(entry, nextSeq));
    nextSeq += 1;
  }
  if (entries.length > 0) schedule();
}

/**
 * Tell the buffer whether the reader is pinned to the bottom.
 *
 * The LogStream calls this as the viewport crosses the 50px stick-to-bottom threshold. It
 * only changes the flush cadence — no line is ever dropped because of it.
 */
export function setFollowing(value: boolean): void {
  if (following === value) return;
  following = value;
  if (pending.length > 0) schedule();
}

/** Whether the buffer is flushing at frame rate. */
export function isFollowing(): boolean {
  return following;
}

/** Empty the ring. The `clear` action in the LogStream toolbar. */
export function clearLogs(): void {
  ring = [];
  pending = [];
  snapshot = [];
  emit();
}

/** Subscribe to flushes. The `subscribe` argument of `useSyncExternalStore`. */
export function subscribeLogs(listener: Listener): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/**
 * The current lines.
 *
 * Returns a stable reference between flushes, which `useSyncExternalStore` requires: a fresh
 * array on every call would re-render on every unrelated state change and, in React 18+,
 * throw a "getSnapshot should be cached" error.
 */
export function getLogsSnapshot(): readonly LogLine[] {
  return snapshot;
}

/** Server snapshot for the same store. There is no SSR here; the tail simply starts empty. */
export function getLogsServerSnapshot(): readonly LogLine[] {
  return snapshot;
}
