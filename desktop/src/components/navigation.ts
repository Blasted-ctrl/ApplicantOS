/**
 * The destination table (`docs/UI.md` §5.3 and §9.3).
 *
 * One list, read by three surfaces: the sidebar renders it, the command palette's `Go to`
 * group renders it, and the shell's chord handler dispatches to it. That is the structural
 * form of §12.3's rule — *no action exists only as a shortcut* — because a destination cannot
 * be added here without appearing in the palette, and cannot appear in the palette without a
 * visible sidebar row.
 *
 * The grouping is by **lifecycle, not recency**: what the agent is doing for you now (WORK),
 * what it knows about you (LIBRARY), and how it is behaving (SYSTEM). `Settings` is pinned to
 * the footer and therefore carries no group.
 *
 * Kept out of `router.tsx` on purpose: the router imports every route module, every route
 * module imports components, and a component importing the router back would close a cycle
 * that resolves differently under Vite than under `tsc`.
 */

import {
  BarChart3,
  Brain,
  Briefcase,
  ClipboardCheck,
  FileText,
  Files,
  LayoutDashboard,
  Mail,
  PlayCircle,
  ScrollText,
  Settings,
  type LucideIcon,
} from 'lucide-react';

/**
 * Every path the shell can navigate to.
 *
 * Declared as a literal union rather than `string` so that a typo in a sidebar row is a
 * compile error against the route tree instead of a silent 404 at runtime.
 */
export const APP_PATHS = [
  '/',
  '/applications',
  '/postings',
  '/reviews',
  '/knowledge',
  '/resumes',
  '/sessions',
  '/analytics',
  '/tracking',
  '/logs',
  '/settings',
  '/onboarding',
] as const;

/** One of the shell's destinations. */
export type AppPath = (typeof APP_PATHS)[number];

/** Sidebar groups, in render order. `null` means the pinned footer. */
export type NavGroup = 'Work' | 'Library' | 'System';

/** One sidebar row / palette destination. */
export interface Destination {
  /** Route path. */
  readonly path: AppPath;
  readonly label: string;
  readonly icon: LucideIcon;
  readonly group: NavGroup;
  /** Shortcut id in `lib/shortcuts.ts`, when the `g` namespace covers this destination. */
  readonly shortcutId?: string;
  /** One line for the command palette, so the palette is browsable rather than only searchable. */
  readonly hint: string;
}

/** The nine grouped destinations, in sidebar order. */
export const DESTINATIONS: readonly Destination[] = [
  {
    path: '/',
    label: 'Dashboard',
    icon: LayoutDashboard,
    group: 'Work',
    shortcutId: 'go.dashboard',
    hint: 'What the agent did overnight',
  },
  {
    path: '/reviews',
    label: 'Review queue',
    icon: ClipboardCheck,
    group: 'Work',
    shortcutId: 'go.reviews',
    hint: 'Applications the agent refused to guess at',
  },
  {
    path: '/applications',
    label: 'Applications',
    icon: FileText,
    group: 'Work',
    shortcutId: 'go.applications',
    hint: 'Every application and its proof of submission',
  },
  {
    path: '/postings',
    label: 'Postings',
    icon: Briefcase,
    group: 'Work',
    shortcutId: 'go.postings',
    hint: 'Discovered roles and how they scored',
  },
  {
    path: '/knowledge',
    label: 'Knowledge',
    icon: Brain,
    group: 'Library',
    shortcutId: 'go.knowledge',
    hint: 'Sources, facts and the entity graph',
  },
  {
    path: '/resumes',
    label: 'Résumés',
    icon: Files,
    group: 'Library',
    shortcutId: 'go.resumes',
    hint: 'Variants, versions and the facts behind each bullet',
  },
  {
    path: '/sessions',
    label: 'Runs',
    icon: PlayCircle,
    group: 'System',
    shortcutId: 'go.sessions',
    hint: 'Run history and the live run',
  },
  {
    path: '/analytics',
    label: 'Analytics',
    icon: BarChart3,
    group: 'System',
    shortcutId: 'go.analytics',
    hint: 'Funnel, outcomes and observational insights',
  },
  {
    path: '/tracking',
    label: 'Status sync',
    icon: Mail,
    group: 'System',
    hint: 'Connected mailboxes and the signal queue',
  },
  {
    path: '/logs',
    label: 'Logs',
    icon: ScrollText,
    group: 'System',
    shortcutId: 'go.logs',
    hint: 'The live log stream and historical search',
  },
];

/** The pinned footer destination. */
export const SETTINGS_DESTINATION: Destination = {
  path: '/settings',
  label: 'Settings',
  icon: Settings,
  group: 'System',
  shortcutId: 'go.settings',
  hint: 'Safety switches, matching, providers and appearance',
};

/** Groups in sidebar order. */
export const NAV_GROUPS: readonly NavGroup[] = ['Work', 'Library', 'System'];

/**
 * Whether a sidebar row is the active one.
 *
 * Prefix matching, with the dashboard special-cased: `/` is a prefix of everything, so a
 * naïve `startsWith` would light the Dashboard row on every screen in the product.
 */
export function isActivePath(destination: AppPath, pathname: string): boolean {
  if (destination === '/') return pathname === '/';
  return pathname === destination || pathname.startsWith(`${destination}/`);
}
