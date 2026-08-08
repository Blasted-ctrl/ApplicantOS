/**
 * The keyboard cheatsheet (`docs/UI.md` §9, §12.3).
 *
 * Rendered straight from `lib/shortcuts.ts`, grouped exactly as that file groups it. That is
 * the whole design: a binding cannot exist without appearing here, because both this dialog
 * and the command palette read the same array. The rule "no action exists only as a shortcut"
 * is therefore structural rather than a review comment.
 *
 * Destructive bindings are marked in the list. `Ctrl` means irreversible across the product
 * (§9.1), and a cheatsheet that showed `Ctrl+X` next to `x` with no distinction would be
 * teaching the wrong lesson.
 */

import { useMemo, useState } from 'react';

import { Dialog, DialogContent, KbdCombo, SearchInput } from '@/components/ui';
import { SHORTCUT_GROUPS, SHORTCUTS, type ShortcutGroup } from '@/lib/shortcuts';

/** Props for {@link ShortcutsDialog}. */
export interface ShortcutsDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

/** Every binding in the product, searchable. */
export function ShortcutsDialog({ open, onOpenChange }: ShortcutsDialogProps) {
  const [query, setQuery] = useState('');

  const grouped = useMemo(() => {
    const needle = query.trim().toLowerCase();
    const matches = SHORTCUTS.filter(
      (shortcut) => needle === '' || shortcut.description.toLowerCase().includes(needle),
    );
    return SHORTCUT_GROUPS.map((group: ShortcutGroup) => ({
      group,
      items: matches.filter((shortcut) => shortcut.group === group),
    })).filter((section) => section.items.length > 0);
  }, [query]);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        open={open}
        size="lg"
        title="Keyboard"
        description="Every binding in ApplicantOS. Unmodified letters act on the focused row; g is the navigation namespace; Ctrl means irreversible."
      >
        <SearchInput
          value={query}
          onChange={(event) => {
            setQuery(event.target.value);
          }}
          placeholder="Search bindings…"
          aria-label="Search keyboard shortcuts"
          className="mb-4"
        />

        {grouped.length === 0 ? (
          <p className="py-8 text-center text-sm text-muted">
            No binding matches “{query}”.
          </p>
        ) : (
          <div className="flex flex-col gap-5">
            {grouped.map((section) => (
              <section key={section.group}>
                <h3 className="label-caps mb-1.5">{section.group}</h3>
                <ul className="flex flex-col">
                  {section.items.map((shortcut) => (
                    <li
                      key={shortcut.id}
                      className="flex h-7 items-center justify-between gap-4 border-b border-state-divider last:border-b-0"
                    >
                      <span className="min-w-0 truncate text-sm text-secondary">
                        {shortcut.description}
                        {shortcut.destructive === true && (
                          <span className="ml-2 text-mini text-st-danger">irreversible</span>
                        )}
                      </span>
                      <KbdCombo combo={shortcut.combo} />
                    </li>
                  ))}
                </ul>
              </section>
            ))}
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
