/**
 * Toaster — `sonner`, skinned to our tokens (`docs/UI.md` §7.15).
 *
 * Sonner rather than shadcn's deprecated `toast`, for a specific reason stated in §6.4: its
 * enter and exit are **CSS transitions, not keyframes**, so rapid stacking *retargets*
 * smoothly instead of restarting each toast's animation from the top. During a run, four
 * toasts can arrive in a second, and that difference is the whole feel of it.
 *
 * Bottom-right, at most three visible, 356px wide, level 4 elevation. Timers pause when the
 * window is hidden or the stack is hovered — a notification the user was not present for has
 * not been seen.
 *
 * **What may fire a toast is a closed list** (§7.15), and it is enforced in `lib/notify.ts`
 * rather than here: this component only renders them. In particular, a successful optimistic
 * mutation does **not** toast — the UI already showed the result.
 */

import { CheckCircle2, CircleAlert, Info, TriangleAlert } from 'lucide-react';
import { Toaster as SonnerToaster } from 'sonner';

/** Props for {@link Toaster}. */
export type ToasterProps = React.ComponentProps<typeof SonnerToaster>;

/**
 * Mount once, inside the app shell.
 *
 * `theme="system"` is deliberate even though the app has its own theme store: Sonner uses it
 * only to pick a default palette, and every colour below is overridden by a token, so the
 * remaining effect is zero. Passing the resolved theme would mean re-rendering the whole
 * toaster on a theme change for no visual difference.
 */
export function Toaster(props: ToasterProps) {
  return (
    <SonnerToaster
      position="bottom-right"
      visibleToasts={3}
      gap={8}
      offset={16}
      icons={{
        success: <CheckCircle2 className="size-3.5 text-st-success" aria-hidden="true" />,
        error: <CircleAlert className="size-3.5 text-st-danger" aria-hidden="true" />,
        warning: <TriangleAlert className="size-3.5 text-st-review" aria-hidden="true" />,
        info: <Info className="size-3.5 text-accent-text" aria-hidden="true" />,
      }}
      toastOptions={{
        classNames: {
          toast:
            'group w-[356px] rounded-lg border border-strong bg-overlay p-3 text-sm text-primary shadow-float',
          title: 'text-sm font-medium text-primary',
          // The correlation id lives here; mono because it is an id (§3.5).
          description: 'mt-0.5 font-mono text-micro tracking-normal text-muted',
          actionButton:
            'ml-auto inline-flex h-[26px] shrink-0 items-center rounded-md px-2 text-sm font-medium text-accent-text hover:bg-state-hover',
          cancelButton:
            'inline-flex h-[26px] shrink-0 items-center rounded-md px-2 text-sm text-secondary hover:bg-state-hover',
          closeButton: 'border-strong bg-overlay text-secondary',
        },
      }}
      {...props}
    />
  );
}
