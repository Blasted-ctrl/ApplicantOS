/**
 * The command palette, wired (`docs/UI.md` §7.18).
 *
 * **Filtering is local and reads only what the cache already holds.** The applications and
 * postings groups consume the very same query keys `/applications` and `/postings` render
 * from, so opening the palette costs zero requests and the results are the rows the user was
 * just looking at. A palette that waits on the network is a palette people stop opening.
 *
 * **Every action row shows its own binding.** That is the only mechanism that reliably
 * graduates a user from searching to muscle memory, and it is why the palette is the
 * discoverable path to every action in the product (§12.3).
 *
 * Destructive actions appear here too — `Stop the run` — but they route through the same
 * confirmation the keyboard binding does. The palette is a way to *reach* an action, never a
 * way to skip its safeguards.
 */

import { useNavigate } from '@tanstack/react-router';
import {
  Command as CommandIcon,
  Keyboard,
  Play,
  RotateCw,
  Search,
  Square,
  Sun,
  Trash2,
} from 'lucide-react';
import { useCallback, useState } from 'react';

import {
  CommandGroup,
  CommandItem,
  CommandPalette,
  Kbd,
  KbdCombo,
  StatusDot,
} from '@/components/ui';
import { useApplications, usePostings } from '@/hooks';
import { useActiveSession } from '@/hooks/use-sessions';
import { SHORTCUTS_BY_ID } from '@/lib/shortcuts';
import { orDash, providerLabel, statusLabel } from '@/lib/utils';
import { useUiStore } from '@/stores/ui';

import { DESTINATIONS, SETTINGS_DESTINATION } from './navigation';

/** How many cached entities each entity group offers. The palette is a shortcut, not a list. */
const ENTITY_LIMIT = 6;

/** Props for {@link CommandMenu}. */
export interface CommandMenuProps {
  onStartRun: () => void;
  onStopRun: () => void;
  onDiscover: () => void;
  onResetCache: () => void;
  onShowShortcuts: () => void;
}

/** The `⌘K` surface. */
export function CommandMenu({
  onStartRun,
  onStopRun,
  onDiscover,
  onResetCache,
  onShowShortcuts,
}: CommandMenuProps) {
  const open = useUiStore((state) => state.paletteOpen);
  const setPaletteOpen = useUiStore((state) => state.setPaletteOpen);
  const cycleTheme = useUiStore((state) => state.cycleTheme);
  const navigate = useNavigate();
  const [query, setQuery] = useState('');
  const activeSession = useActiveSession();

  const { data: applications } = useApplications({});
  const { data: postings } = usePostings({});

  const run = useCallback(
    (action: () => void) => {
      setPaletteOpen(false);
      setQuery('');
      action();
    },
    [setPaletteOpen],
  );

  const shortcut = (id: string) => {
    const combo = SHORTCUTS_BY_ID.get(id)?.combo;
    return combo === undefined ? undefined : <KbdCombo combo={combo} />;
  };

  return (
    <CommandPalette
      open={open}
      onOpenChange={(next) => {
        setPaletteOpen(next);
        if (!next) setQuery('');
      }}
      value={query}
      onValueChange={setQuery}
      footer={
        <>
          <span className="inline-flex items-center gap-1">
            <Kbd keys={['↑']} />
            <Kbd keys={['↓']} />
            navigate
          </span>
          <span className="inline-flex items-center gap-1">
            <Kbd keys={['Enter']} />
            open
          </span>
          <span className="inline-flex items-center gap-1">
            <Kbd keys={['Esc']} />
            dismiss
          </span>
        </>
      }
    >
      {(applications?.items ?? []).slice(0, ENTITY_LIMIT).length > 0 && (
        <CommandGroup heading="Applications">
          {(applications?.items ?? []).slice(0, ENTITY_LIMIT).map((application) => (
            <CommandItem
              key={application.id}
              value={`${application.company?.name ?? ''} ${application.posting?.title ?? ''} ${application.id}`}
              icon={<StatusDot status={application.status} />}
              meta={statusLabel(application.status)}
              onSelect={() => {
                run(() => {
                  void navigate({
                    to: '/applications/$id',
                    params: { id: application.id },
                  });
                });
              }}
            >
              {orDash(application.company?.name)} · {orDash(application.posting?.title)}
            </CommandItem>
          ))}
        </CommandGroup>
      )}

      {(postings?.items ?? []).slice(0, ENTITY_LIMIT).length > 0 && (
        <CommandGroup heading="Postings">
          {(postings?.items ?? []).slice(0, ENTITY_LIMIT).map((posting) => (
            <CommandItem
              key={posting.id}
              value={`${posting.title} ${posting.company?.name ?? ''} ${posting.provider}`}
              icon={<Search aria-hidden="true" />}
              meta={providerLabel(posting.provider)}
              onSelect={() => {
                run(() => {
                  void navigate({ to: '/postings' });
                });
              }}
            >
              {posting.title} · {orDash(posting.company?.name)}
            </CommandItem>
          ))}
        </CommandGroup>
      )}

      <CommandGroup heading="Actions">
        <CommandItem
          value="start a run automation"
          icon={<Play aria-hidden="true" />}
          shortcut={shortcut('session.start')}
          onSelect={() => {
            run(onStartRun);
          }}
        >
          Start a run
        </CommandItem>
        <CommandItem
          value="stop the running session"
          icon={<Square aria-hidden="true" />}
          shortcut={shortcut('session.stop')}
          disabled={activeSession === null}
          onSelect={() => {
            run(onStopRun);
          }}
        >
          Stop the run
        </CommandItem>
        <CommandItem
          value="discover postings search jobs"
          icon={<Search aria-hidden="true" />}
          shortcut={shortcut('postings.discover')}
          onSelect={() => {
            run(onDiscover);
          }}
        >
          Discover postings
        </CommandItem>
        <CommandItem
          value="toggle theme dark light system appearance"
          icon={<Sun aria-hidden="true" />}
          shortcut={shortcut('theme.toggle')}
          onSelect={() => {
            run(cycleTheme);
          }}
        >
          Cycle the theme
        </CommandItem>
        <CommandItem
          value="keyboard shortcuts cheatsheet help"
          icon={<Keyboard aria-hidden="true" />}
          shortcut={shortcut('help.shortcuts')}
          onSelect={() => {
            run(onShowShortcuts);
          }}
        >
          Keyboard cheatsheet
        </CommandItem>
        <CommandItem
          value="reset local cache clear storage"
          icon={<Trash2 aria-hidden="true" />}
          onSelect={() => {
            run(onResetCache);
          }}
        >
          Reset the local cache
        </CommandItem>
        <CommandItem
          value="reload the window"
          icon={<RotateCw aria-hidden="true" />}
          onSelect={() => {
            run(() => {
              window.location.reload();
            });
          }}
        >
          Reload the window
        </CommandItem>
      </CommandGroup>

      <CommandGroup heading="Go to">
        {[...DESTINATIONS, SETTINGS_DESTINATION].map((destination) => (
          <CommandItem
            key={destination.path}
            value={`${destination.label} ${destination.hint}`}
            icon={<CommandIcon aria-hidden="true" />}
            meta={destination.hint}
            {...(destination.shortcutId === undefined
              ? {}
              : { shortcut: shortcut(destination.shortcutId) })}
            onSelect={() => {
              run(() => {
                void navigate({ to: destination.path });
              });
            }}
          >
            {destination.label}
          </CommandItem>
        ))}
      </CommandGroup>
    </CommandPalette>
  );
}
