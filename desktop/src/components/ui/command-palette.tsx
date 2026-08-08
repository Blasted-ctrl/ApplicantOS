/**
 * CommandPalette — `cmdk` inside a Radix `Dialog` (`docs/UI.md` §7.18).
 *
 * ```
 * ┌─────────────────────────────────────────────────────────────┐  640px, --radius-xl
 * │ ⌕  apply to stripe                                     Esc  │  48px input
 * ├─────────────────────────────────────────────────────────────┤
 * │  APPLICATIONS                                               │  .label-caps, 24px
 * │  ○  Stripe · Senior Backend Engineer      Submitted    ↵     │  40px row
 * │  ACTIONS                                                    │
 * │  ▸  Start a run                                    ⌘ ⇧ R    │
 * └─────────────────────────────────────────────────────────────┘
 *    ↑↓ navigate   ↵ open   ⌘↵ open in new sheet   ⌃X delete
 * ```
 *
 * **Motion: NONE.** Open and close are instant, both scrim and panel. This surface is used
 * dozens of times a day, and §6.4 gives anything at that frequency `--dur-0` — Raycast ships
 * no animation here for the same reason. It is the one overlay in the product that does not
 * animate, and that is deliberate rather than unfinished.
 *
 * **Filtering is local, fuzzy, and in-memory, with zero network in the loop.** The palette
 * searches what the cache already holds. A palette that waits on a request is a palette
 * people stop opening.
 *
 * **Every result row shows its own keyboard shortcut inline.** This is the only mechanism
 * that reliably graduates a user from searching to muscle memory, and it is why the palette
 * is *the discoverable path to every action*: no action may exist only as a shortcut.
 *
 * The highlighted row uses `--state-selected` with **no transition** — it has to track arrow
 * keys at key-repeat rate, and a 140ms fade turns a held arrow key into a smear.
 */

import * as DialogPrimitive from '@radix-ui/react-dialog';
import { Command } from 'cmdk';
import { Search } from 'lucide-react';
import type { ReactNode } from 'react';

import { cn } from '@/lib/utils';

import { Kbd } from './kbd';

/** Group order is fixed (§7.18); the palette never reorders its sections by relevance. */
export const PALETTE_GROUPS = [
  'Applications',
  'Postings',
  'Actions',
  'Go to',
  'Recent',
] as const;
export type PaletteGroup = (typeof PALETTE_GROUPS)[number];

/** Props for {@link CommandPalette}. */
export interface CommandPaletteProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  value: string;
  onValueChange: (value: string) => void;
  /** `CommandGroup`s, in {@link PALETTE_GROUPS} order. */
  children: ReactNode;
  /** Modifier hints for the highlighted row, rendered in the 28px footer. */
  footer?: ReactNode;
  placeholder?: string;
}

/** The palette shell: scrim, panel, input, list and footer. */
export function CommandPalette({
  open,
  onOpenChange,
  value,
  onValueChange,
  children,
  footer,
  placeholder = 'Search applications, postings and actions…',
}: CommandPaletteProps) {
  return (
    <DialogPrimitive.Root open={open} onOpenChange={onOpenChange}>
      <DialogPrimitive.Portal>
        {/* No animation on either layer — see the module docstring. */}
        <DialogPrimitive.Overlay className="scrim fixed inset-0 z-50" />
        <DialogPrimitive.Content
          className={cn(
            'accent-hairline fixed left-1/2 top-[18vh] z-50 w-[640px] max-w-[calc(100vw-96px)]',
            '-translate-x-1/2 overflow-hidden rounded-xl border border-strong bg-overlay shadow-dialog',
          )}
          aria-label="Command palette"
        >
          <DialogPrimitive.Title className="sr-only">Command palette</DialogPrimitive.Title>
          <DialogPrimitive.Description className="sr-only">
            Search applications, postings and actions. Use the arrow keys to navigate and Enter
            to open.
          </DialogPrimitive.Description>

          <Command
            shouldFilter
            loop
            className="flex flex-col"
            // cmdk scores substrings and transpositions; the list is already in memory, so
            // there is nothing to debounce and nothing to await.
            filter={(itemValue, search) =>
              itemValue.toLowerCase().includes(search.toLowerCase()) ? 1 : 0
            }
          >
            <div className="flex h-12 items-center gap-2 border-b border-state-divider px-4">
              <Search className="size-4 shrink-0 text-muted" aria-hidden="true" />
              <Command.Input
                value={value}
                onValueChange={onValueChange}
                placeholder={placeholder}
                className="h-full min-w-0 flex-1 bg-transparent text-md text-primary outline-none placeholder:text-muted"
              />
              <Kbd keys={['Esc']} />
            </div>

            <Command.List className="scroll-region max-h-[min(420px,50vh)] p-1">
              <Command.Empty className="px-3 py-8 text-center text-sm text-muted">
                No matches
              </Command.Empty>
              {children}
            </Command.List>

            {footer !== undefined && (
              <div className="flex h-7 items-center gap-3 border-t border-state-divider bg-inset px-3 text-micro tracking-normal text-muted">
                {footer}
              </div>
            )}
          </Command>
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
}

/** Props for {@link CommandGroup}. */
export interface CommandGroupProps {
  heading: PaletteGroup;
  children: ReactNode;
}

/** A section of results, headed by a `.label-caps` eyebrow. */
export function CommandGroup({ heading, children }: CommandGroupProps) {
  return (
    <Command.Group
      heading={heading}
      className={cn(
        '[&_[cmdk-group-heading]]:label-caps [&_[cmdk-group-heading]]:flex',
        '[&_[cmdk-group-heading]]:h-6 [&_[cmdk-group-heading]]:items-center [&_[cmdk-group-heading]]:px-2',
      )}
    >
      {children}
    </Command.Group>
  );
}

/** Props for {@link CommandItem}. */
export interface CommandItemProps {
  /** Everything this row should match on: the title, the company, the id. */
  value: string;
  onSelect: () => void;
  /** 14px leading icon, or a `StatusDot` for an entity row. */
  icon?: ReactNode;
  children: ReactNode;
  /** Right-aligned metadata — a status word, a score. */
  meta?: ReactNode;
  /** The row's own shortcut. Required for action rows; this is what teaches the keymap. */
  shortcut?: ReactNode;
  disabled?: boolean;
}

/** One 40px result row. */
export function CommandItem({
  value,
  onSelect,
  icon,
  children,
  meta,
  shortcut,
  disabled = false,
}: CommandItemProps) {
  return (
    <Command.Item
      value={value}
      onSelect={onSelect}
      disabled={disabled}
      className={cn(
        'flex h-10 cursor-default select-none items-center gap-2 rounded-md px-2',
        'text-base text-primary outline-none',
        // No transition: the highlight must keep up with a held arrow key.
        'data-[selected=true]:bg-state-selected',
        'data-[disabled=true]:pointer-events-none data-[disabled=true]:text-disabled',
      )}
    >
      {icon !== undefined && (
        <span className="inline-flex size-3.5 shrink-0 items-center justify-center text-muted">
          {icon}
        </span>
      )}
      <span className="min-w-0 flex-1 truncate">{children}</span>
      {meta !== undefined && <span className="shrink-0 text-mini text-muted">{meta}</span>}
      {shortcut}
    </Command.Item>
  );
}
