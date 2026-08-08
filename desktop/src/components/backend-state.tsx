/**
 * The backend-process banner (`docs/CONTRACTS.md` §18).
 *
 * The Python API is a child process, which gives this app a state a web client never has: the
 * server can be *starting*. That distinction is the whole reason this component exists —
 * "starting" deserves a quiet strip and no alarm, while "failed" deserves the reason, the tail
 * of the process's own output, and a way to try again.
 *
 * It is a strip under the titlebar rather than a takeover, deliberately. The cache is
 * persisted, so with a dead backend the app still renders every screen the user visited
 * yesterday; blanking that behind a modal would throw away the one thing the caching strategy
 * bought.
 */

import { AlertTriangle, RotateCw } from 'lucide-react';
import { useCallback, useState } from 'react';

import { Button } from '@/components/ui';
import { reconnectEvents } from '@/lib/query/ws';
import { resetBaseUrl, restartBackend } from '@/lib/tauri';
import { useSessionStore } from '@/stores/session';

/** Copy per non-serving phase. `ready` never reaches this component. */
const PHASE_COPY = {
  starting: {
    title: 'Starting the local backend',
    body: 'Everything on screen is served from the local cache until it answers.',
    tone: 'progress' as const,
  },
  failed: {
    title: 'The local backend could not start',
    body: 'Cached data is still readable, but nothing can be discovered, scored or submitted.',
    tone: 'danger' as const,
  },
  stopped: {
    title: 'The local backend has stopped',
    body: 'Cached data is still readable. Restart it to resume discovery and applications.',
    tone: 'danger' as const,
  },
};

/** A strip under the titlebar, present only while the backend is not serving. */
export function BackendBanner() {
  const backend = useSessionStore((state) => state.backend);
  const [restarting, setRestarting] = useState(false);

  const restart = useCallback(() => {
    setRestarting(true);
    void restartBackend()
      .then(() => {
        resetBaseUrl();
        reconnectEvents();
      })
      .finally(() => {
        setRestarting(false);
      });
  }, []);

  // `null` is browser development, where there is no shell to report a phase and the backend
  // is whatever the developer started by hand.
  if (backend === null || backend.phase === 'ready') return null;

  const copy = PHASE_COPY[backend.phase];
  const danger = copy.tone === 'danger';

  return (
    <div
      role={danger ? 'alert' : 'status'}
      className="flex shrink-0 flex-col gap-1 border-b px-6 py-2"
      style={{
        backgroundColor: danger ? 'rgb(232 91 91 / 0.10)' : 'rgb(90 140 240 / 0.08)',
        borderBottomColor: danger ? 'rgb(232 91 91 / 0.32)' : 'var(--border-subtle)',
      }}
    >
      <div className="flex items-center gap-2">
        {danger && (
          <AlertTriangle className="size-3.5 shrink-0 text-st-danger" aria-hidden="true" />
        )}
        <span className="text-sm font-medium text-primary">{copy.title}</span>
        <span className="min-w-0 flex-1 truncate text-sm text-secondary">{copy.body}</span>
        {backend.managed && (
          <Button
            variant="secondary"
            size="sm"
            loading={restarting}
            leadingIcon={<RotateCw aria-hidden="true" />}
            onClick={restart}
          >
            Restart backend
          </Button>
        )}
      </div>

      {backend.message !== null && (
        <p className="font-mono text-mini text-secondary">{backend.message}</p>
      )}

      {backend.detail.length > 0 && (
        <pre className="max-h-24 overflow-auto whitespace-pre-wrap rounded-sm bg-inset p-2 font-mono text-micro tracking-normal text-muted">
          {backend.detail.join('\n')}
        </pre>
      )}
    </div>
  );
}
