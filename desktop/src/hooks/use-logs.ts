/**
 * Historical log search.
 *
 * This is `GET /logs` — a normal paginated query with a 10-second `staleTime`, never
 * persisted. The **live tail is not here**: it is the ring buffer in `lib/log-buffer.ts`,
 * read through `use-log-stream.ts`. `docs/UI.md` §7.21 rule 1 is explicit about why — an
 * append-per-line query entry churns the cache and triggers the persister on every line, and
 * a busy run emits hundreds of lines a second.
 *
 * The two are complementary rather than redundant: the tail answers "what is happening now",
 * this answers "what happened at 09:41 in the run that failed".
 */

import { useQuery } from '@tanstack/react-query';

import type { LogFilters } from '@/lib/api/types';
import { logsOptions } from '@/lib/query/options';

/** `GET /logs`. */
export function useLogs(filters: LogFilters = {}) {
  return useQuery(logsOptions(filters));
}

/**
 * The log lines for one correlation id.
 *
 * The reason every error toast carries its correlation id: this turns that string into the
 * exact lines the failing request produced, with no searching.
 */
export function useCorrelatedLogs(correlationId: string | undefined) {
  return useQuery({
    ...logsOptions(correlationId === undefined ? {} : { correlation_id: correlationId }),
    enabled: correlationId !== undefined && correlationId !== '',
  });
}
