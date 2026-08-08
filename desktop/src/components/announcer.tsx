/**
 * The single shared live region (`docs/UI.md` §12.5).
 *
 * Mounted once, in the root route. Its imperative half is `components/announce.ts`, and the
 * rule the pair enforces is surgical: **only a change to the row the user is focused on is
 * announced.** Unfocused rows, stat tiles and chart data update silently, because forty rows
 * announcing themselves during a run is indistinguishable from noise.
 */

import { useEffect, useRef } from 'react';

import { registerAnnouncer } from './announce';

/** The shared polite live region. Mount exactly once. */
export function Announcer() {
  const ref = useRef<HTMLParagraphElement | null>(null);

  useEffect(() => {
    registerAnnouncer(ref.current);
    return () => {
      registerAnnouncer(null);
    };
  }, []);

  return (
    <p
      ref={ref}
      id="a11y-announcer"
      aria-live="polite"
      aria-atomic="true"
      className="sr-only"
    />
  );
}
