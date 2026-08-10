/**
 * Toasts, and the four things they are allowed to say (`docs/UI.md` §7.15).
 *
 * The rule is binding, so it is expressed as four functions rather than as a comment above a
 * general-purpose `toast()`:
 *
 * 1. a mutation **failed** — with a Retry action,
 * 2. an action is **undoable** — `Application archived · Undo`, eight seconds,
 * 3. a **background job finished** while the user was elsewhere,
 * 4. a **file was written to disk** — with a Reveal action.
 *
 * **A successful optimistic mutation does not toast.** The UI already showed the result; a
 * toast saying so is a second notification of something the user watched happen, and at forty
 * applications a night it is noise that trains people to ignore the corner of the screen where
 * the failures also appear.
 *
 * Errors carry their correlation id in the description. That is what turns a screenshot of a
 * toast into a log query — `GET /logs?correlation_id=…` finds the exact lines that produced
 * it — and it costs one line of small mono text.
 */

import { degradedReason, isDegraded } from '@/lib/api/dispatch';
import type { OkResponse } from '@/lib/api/types';

import { toast } from 'sonner';

import { ApiError } from '@/lib/api/client';

/** How long an undo toast stays. Long enough to reconsider, short enough not to linger. */
const UNDO_DURATION_MS = 8_000;

/** Human-readable text for any thrown value, without leaking internals. */
function describe(error: unknown): string | undefined {
  if (error instanceof ApiError) {
    const detail = error.detail ?? error.message;
    return error.correlationId === '' ? detail : `${detail} · ${error.correlationId}`;
  }
  if (error instanceof Error) return error.message;
  return undefined;
}

/**
 * Case 1 — a mutation failed.
 *
 * @param title - What did not happen, in the user's terms: "Could not update application".
 *   Never the error code; the code lives in the description where it is useful and invisible.
 * @param onRetry - Supplied when retrying could plausibly work. Omitted for a 4xx, where the
 *   same request would fail the same way and a Retry button is a lie.
 */
export function notifyError(title: string, error: unknown, onRetry?: () => void): void {
  const retryable = !(error instanceof ApiError) || error.isRetryable;
  toast.error(title, {
    description: describe(error),
    ...(onRetry !== undefined && retryable
      ? { action: { label: 'Retry', onClick: onRetry } }
      : {}),
  });
}

/**
 * Case 2 — an undoable action.
 *
 * @param message - Past tense, naming the object: `Application archived`.
 * @param onUndo - Reverses it. Must be safe to call after the toast has already expired.
 */
export function notifyUndo(message: string, onUndo: () => void): void {
  toast(message, {
    duration: UNDO_DURATION_MS,
    action: { label: 'Undo', onClick: onUndo },
  });
}

/**
 * Case 3 — a background job finished while the user was elsewhere.
 *
 * @param message - `Run finished · 12 applied`.
 * @param onOpen - Takes the user to the result, when there is somewhere to go.
 */
export function notifyJobFinished(message: string, onOpen?: () => void): void {
  toast.success(message, {
    ...(onOpen === undefined ? {} : { action: { label: 'View', onClick: onOpen } }),
  });
}

/**
 * Case 4 — a file was written to disk.
 *
 * @param filename - What was written, not where. The path is the Reveal action's job.
 * @param onReveal - Opens the containing folder through the shell.
 */
export function notifyFileWritten(filename: string, onReveal?: () => void): void {
  toast.success('Saved to disk', {
    description: filename,
    ...(onReveal === undefined ? {} : { action: { label: 'Reveal', onClick: onReveal } }),
  });
}

/**
 * A warning that is not a failure — the review queue grew, a provider rate-limited a poll.
 *
 * Deliberately not a fifth case: it is the `warning` *variant* of case 3, for a background
 * job that finished in a state the user should know about. Nothing user-initiated may use it.
 */
export function notifyWarning(message: string, description?: string): void {
  toast.warning(message, description === undefined ? {} : { description });
}

/**
 * Warn when a triggered action did not actually start anywhere.
 *
 * The one case a user must never be left to infer. `POST /sessions/start` and its siblings
 * answer `202` whether or not anything will execute the work, and a UI that shows only the
 * happy path turns "nothing is running this" into a spinner that never resolves. The backend
 * supplies the sentence; this puts it on screen.
 *
 * Silent when the work is running — including when it runs inside the desktop backend rather
 * than on a Celery worker, which is the normal posture and not a degradation.
 *
 * @param response - The `OkResponse` returned by the triggering endpoint.
 * @param action - What the user just did, e.g. `"The run"`. Used as the message subject.
 */
export function notifyIfNotStarted(response: OkResponse | undefined | null, action: string): void {
  if (!isDegraded(response)) return;
  notifyWarning(
    `${action} has not started`,
    degradedReason(response) ??
      'No background worker is available, so nothing is driving this yet.',
  );
}
