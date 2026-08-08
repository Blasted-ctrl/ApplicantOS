/**
 * The event stream's connection state, and the boot-time health check.
 *
 * The socket lives outside React (`lib/query/ws.ts`) so it survives Fast Refresh and
 * StrictMode's double mount without costing a real reconnect. `useSyncExternalStore` is how a
 * component reads it without the store having to know React exists.
 */

import { useQuery } from '@tanstack/react-query';
import { useSyncExternalStore } from 'react';

import {
  getConnectionState,
  subscribeConnection,
  type ConnectionState,
} from '@/lib/query/ws';
import { healthOptions } from '@/lib/query/options';

/** The live event stream's state, for the status bar's dot. */
export function useConnectionState(): ConnectionState {
  return useSyncExternalStore(subscribeConnection, getConnectionState, getConnectionState);
}

/**
 * `GET /health`.
 *
 * `retry: false` on this query specifically: when the backend is down, the point is to find
 * out quickly and render the error state, not to spend eight seconds of exponential backoff
 * before admitting it.
 */
export function useHealth() {
  return useQuery(healthOptions());
}
