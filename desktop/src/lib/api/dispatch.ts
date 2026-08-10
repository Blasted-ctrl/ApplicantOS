/**
 * Reading where a triggered task actually went.
 *
 * Every endpoint that starts background work answers `202` with an `OkResponse` whose `data`
 * carries the dispatch outcome — `mode`, `degraded`, and a human-readable `reason`. The
 * backend has always sent it. Nothing in the renderer read it.
 *
 * That gap had a cost. Pressing **Start a Run** against an install with no worker wrote a
 * session row, returned `degraded: true` with the sentence *"the background worker is not
 * running, so nothing is driving it yet"*, and `useStartSession` threw the whole response
 * away and closed the dialog. The run then sat at `running` with a spinner beside it, and
 * the app had told the user the opposite of what the server said.
 *
 * `mode` is the field to branch on rather than `degraded` alone, because there are three
 * outcomes and only one of them is bad:
 *
 * - `worker` — a Celery worker took it.
 * - `inline` — the desktop backend is running it itself. Normal, and the default posture.
 * - `none` — nothing is running it. This is the one the user must be told about.
 */

import type { JsonObject, OkResponse } from './types';

/** Where a triggered task is executing. */
export type DispatchMode = 'worker' | 'inline' | 'none';

/** The dispatch outcome carried in an `OkResponse.data`. */
export interface DispatchOutcome {
  /** Task name, e.g. `jobs.poll_all`. */
  task?: string;
  /** The queue it routes to. */
  queue?: string;
  /** Where it went. */
  mode: DispatchMode;
  /** True only when `mode` is `none` — the work did not start anywhere. */
  degraded: boolean;
  /** Why it did not start, phrased for a person. Present when `degraded`. */
  reason?: string;
}

/**
 * Pull the dispatch outcome out of an `OkResponse`, if it carries one.
 *
 * Tolerant on purpose: several endpoints merge other keys into the same `data` object, and
 * one (`POST /postings/discover`) returns an array of outcomes. A response without a
 * recognisable outcome yields `null` rather than a fabricated "everything is fine" — callers
 * must not be able to mistake "no information" for "dispatched".
 */
export function readDispatch(response: OkResponse | undefined | null): DispatchOutcome | null {
  const data: JsonObject | undefined = response?.data;
  if (data === undefined || data === null) return null;

  const raw = data as Record<string, unknown>;
  const mode = raw['mode'];
  const degraded = raw['degraded'];

  if (typeof mode !== 'string' && typeof degraded !== 'boolean') return null;

  const resolvedMode: DispatchMode =
    mode === 'worker' || mode === 'inline' || mode === 'none'
      ? mode
      : degraded === true
        ? 'none'
        : 'worker';

  return {
    ...(typeof raw['task'] === 'string' ? { task: raw['task'] } : {}),
    ...(typeof raw['queue'] === 'string' ? { queue: raw['queue'] } : {}),
    mode: resolvedMode,
    degraded: typeof degraded === 'boolean' ? degraded : resolvedMode === 'none',
    ...(typeof raw['reason'] === 'string' ? { reason: raw['reason'] } : {}),
  };
}

/**
 * Key under which `POST /postings/discover` nests its per-provider outcomes.
 *
 * Every other endpoint spreads the outcome across the top level of `data`; discovery is the
 * one that fans out, so it reports an array (`app/api/routes/postings.py`). Reading the
 * wrong key here would not fail loudly — it would silently find no outcomes and report a
 * degraded discovery as fine, which is the exact class of bug this module exists to end.
 */
const FANOUT_KEY = 'tasks';

/**
 * The dispatch outcomes in a response that reports several.
 *
 * A single provider failing to start is worth saying even when the others started, so the
 * array is read rather than only its first element.
 */
export function readDispatches(response: OkResponse | undefined | null): DispatchOutcome[] {
  const data = response?.data as Record<string, unknown> | undefined;
  const fanout = data?.[FANOUT_KEY];
  if (Array.isArray(fanout)) {
    return fanout
      .map((entry) => readDispatch({ ok: true, data: entry as JsonObject }))
      .filter((entry): entry is DispatchOutcome => entry !== null);
  }
  const single = readDispatch(response);
  return single === null ? [] : [single];
}

/** Whether any outcome in a response means "nothing is running this". */
export function isDegraded(response: OkResponse | undefined | null): boolean {
  return readDispatches(response).some((outcome) => outcome.degraded);
}

/** The first stated reason among a response's outcomes, for showing to the user. */
export function degradedReason(response: OkResponse | undefined | null): string | undefined {
  return readDispatches(response).find((outcome) => outcome.degraded)?.reason;
}
