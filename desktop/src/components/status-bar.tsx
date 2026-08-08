/**
 * The 24px status bar (`docs/UI.md` §5.1).
 *
 * The one piece of chrome that is always telling the truth about the machine: whether the
 * event stream is connected, whether a run is executing, when the last one finished, and how
 * much is waiting on a human. Every figure is mono and `tabular-nums`, because these numbers
 * tick while the user is looking at them and a proportional face would reflow the whole bar
 * once a second.
 *
 * It also renders the pending-chord indicator (§9.3): pressing `g` opens a 1200ms window, and
 * for that window the bar lists the second keys that are available. A chord namespace with no
 * visible affordance is a feature only its author can use.
 */

import { useSessions } from '@/hooks';
import { useConnectionState } from '@/hooks/use-connection';
import { useDashboardStats } from '@/hooks/use-analytics';
import { useActiveSession } from '@/hooks/use-sessions';
import { NAVIGATION_CHORDS } from '@/lib/shortcuts';
import { cn, formatDuration, formatRelative, formatTime } from '@/lib/utils';
import { useSessionStore } from '@/stores/session';
import { useUiStore } from '@/stores/ui';

import type { ConnectionState } from '@/lib/query/ws';

/** Dot colour and wording for each connection state. */
const CONNECTION: Readonly<Record<ConnectionState, { color: string; label: string }>> = {
  idle: { color: 'var(--st-neutral)', label: 'Event stream idle' },
  connecting: { color: 'var(--st-progress)', label: 'Connecting to the event stream' },
  open: { color: 'var(--st-success)', label: 'Live' },
  reconnecting: { color: 'var(--st-review)', label: 'Reconnecting' },
  closed: { color: 'var(--st-danger)', label: 'Event stream offline' },
};

/** The always-present bottom bar. */
export function StatusBar() {
  const connection = useConnectionState();
  const activeSession = useActiveSession();
  const indexing = useSessionStore((state) => state.indexing);
  const syncing = useSessionStore((state) => state.syncing);
  const pendingChord = useUiStore((state) => state.pendingChord);
  const { data: stats } = useDashboardStats();
  const { data: sessions } = useSessions({ limit: 1 });

  const lastRun = sessions?.items[0];
  const connectionTone = CONNECTION[connection];

  return (
    <footer className="flex h-6 shrink-0 items-center gap-3 border-t border-subtle bg-chrome px-3 text-mini text-muted">
      <span className="inline-flex shrink-0 items-center gap-1.5" title={connectionTone.label}>
        <span
          aria-hidden="true"
          className="size-1.5 rounded-full"
          style={{ backgroundColor: connectionTone.color }}
        />
        <span className="sr-only">Connection status: </span>
        {activeSession === null ? connectionTone.label : 'Running'}
      </span>

      <Divider />

      {activeSession === null ? (
        <span className="shrink-0 tabular-nums">
          {lastRun === undefined
            ? 'No runs yet'
            : `Last run ${formatTime(lastRun.ended_at ?? lastRun.started_at)} · ${formatDuration(lastRun.duration_seconds)}`}
        </span>
      ) : (
        <span className="shrink-0 tabular-nums">
          {`Run started ${formatRelative(activeSession.started_at)} · ${String(activeSession.applications_completed)} applied · ${String(activeSession.jobs_qualified)} qualified`}
        </span>
      )}

      {indexing !== null && (
        <>
          <Divider />
          <span className="shrink-0 tabular-nums">
            {indexing.progress === null
              ? `Indexing · ${String(indexing.documents)} documents`
              : `Indexing ${String(Math.round(indexing.progress * 100))}%`}
          </span>
        </>
      )}

      {syncing !== null && (
        <>
          <Divider />
          <span className="shrink-0 tabular-nums">
            {`Mailbox sync · ${String(syncing.classified)} classified · ${String(syncing.matched)} matched`}
          </span>
        </>
      )}

      <span className="min-w-0 flex-1" />

      {pendingChord !== null && (
        <span className="inline-flex shrink-0 items-center gap-1.5 text-accent-text">
          <span className="font-mono">{pendingChord} …</span>
          <span className="truncate font-mono text-micro tracking-normal text-muted">
            {[...NAVIGATION_CHORDS.keys()].join(' ')}
          </span>
        </span>
      )}

      {stats !== undefined && (
        <>
          <span className="shrink-0 tabular-nums">
            {`${String(stats.total)} ${stats.total === 1 ? 'application' : 'applications'}`}
          </span>
          <Divider />
          <span
            className={cn('shrink-0 tabular-nums', stats.needs_review > 0 && 'text-st-review')}
          >
            {`${String(stats.needs_review)} need review`}
          </span>
        </>
      )}

      <Divider />
      <span className="shrink-0 font-mono text-micro tracking-normal">v{__APP_VERSION__}</span>
    </footer>
  );
}

/** The `·` that separates the bar's fields. */
function Divider() {
  return (
    <span aria-hidden="true" className="shrink-0 text-disabled">
      ·
    </span>
  );
}
