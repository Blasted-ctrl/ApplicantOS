/**
 * The provider stack. Mounted once, above the router.
 *
 * Four providers, in this order, and the order is not arbitrary:
 *
 * 1. **`QueryClientProvider`** with the module-scope client — the same instance
 *    `hydrateHotSnapshot()` filled *before* `createRoot().render()`. A client created inside
 *    a component would not exist at that point, and the hot snapshot is the entire cold-start
 *    strategy (`docs/UI.md` §10.5).
 * 2. **`MotionConfig reducedMotion="user"`** — layer 1 of the three-layer reduced-motion
 *    policy (§6.7). It disables transform and layout animations while *preserving* opacity
 *    and colour, which is the policy: less and gentler motion, not zero feedback.
 * 3. **`TooltipProvider`** — the 400ms / 300ms skip-delay behaviour is a property of the
 *    whole *group*, not of one tooltip (§7.14), so there is exactly one.
 * 4. **`Toaster`** — inside the tree so its portal inherits the theme attribute.
 *
 * **`useIsRestoring` is deliberately absent.** §10.5 is explicit: never gate the tree on it.
 * Render the full shell immediately and let restoration fill in; gating just moves the
 * blank-then-pop flash earlier in the boot, where it is more visible rather than less.
 */

import { QueryClientProvider } from '@tanstack/react-query';
import { MotionConfig } from 'framer-motion';
import type { ReactNode } from 'react';

import { queryClient } from '@/lib/query/client';

import { Toaster } from './ui/toast';
import { TooltipProvider } from './ui/tooltip';

/** Props for {@link AppProviders}. */
export interface AppProvidersProps {
  children: ReactNode;
}

/** Everything the tree needs, in one element. */
export function AppProviders({ children }: AppProvidersProps) {
  return (
    <QueryClientProvider client={queryClient}>
      <MotionConfig reducedMotion="user">
        <TooltipProvider>
          {children}
          <Toaster />
        </TooltipProvider>
      </MotionConfig>
    </QueryClientProvider>
  );
}
