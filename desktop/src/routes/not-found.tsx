/**
 * The unmatched-route screen.
 *
 * Rare in a desktop app with no address bar, but reachable two ways that both matter: a saved
 * window that reopens on a route a later build removed, and a deep link into an entity that
 * has since been archived. Both deserve a way back rather than a blank pane.
 */

import { useNavigate, useRouterState } from '@tanstack/react-router';

import { Page } from '@/components/page';
import { EmptyState } from '@/components/ui';

/** Nothing matched. */
export function NotFoundRoute() {
  const navigate = useNavigate();
  const pathname = useRouterState({ select: (state) => state.location.pathname });

  return (
    <Page title="Nothing here" subtitle={pathname}>
      <EmptyState
        title="That screen does not exist"
        description="The window may have reopened on a route from an older build, or the record it pointed at has been archived."
        primaryAction={{
          label: 'Go to the dashboard',
          onClick: () => {
            void navigate({ to: '/' });
          },
        }}
      />
    </Page>
  );
}
