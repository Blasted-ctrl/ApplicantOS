/**
 * The hook barrel.
 *
 * One module per query family, plus the five behavioural hooks that implement `docs/UI.md`
 * requirements no screen should re-derive: the skeleton gate (§7.16), prefetch on intent
 * (§10.6), the keymap (§9), the theme's three states (§2.3), and the shell bridge (§18).
 *
 * Data hooks return TanStack results unchanged. That is deliberate: a wrapper that returned
 * `{ data, loading }` would hide `isPending` behind a name that invites branching on the
 * wrong flag, and §10.1 makes `isPending` the *only* value permitted to gate content.
 */

export {
  useApplication,
  useApplicationArtifacts,
  useApplications,
  useArchiveApplication,
  useRetryApplication,
  useSetApplicationStatus,
  useUpdateApplication,
} from './use-applications';
export {
  useAnalyticsOverview,
  useDashboardStats,
  useFunnel,
  useInsights,
  useTimeseries,
} from './use-analytics';
export { useBackendStatus, useMenuActions, useReadySignal } from './use-backend';
export { useConnectionState, useHealth } from './use-connection';
export { useButtonSpinner, useDelayedFlag, type DelayedFlagOptions } from './use-delayed-flag';
export {
  useCreateSource,
  useDeleteSource,
  useEntities,
  useFacts,
  useKnowledgeGraph,
  useKnowledgeSearch,
  useKnowledgeStats,
  useReindex,
  useSources,
  useUpdateFact,
} from './use-knowledge';
export { useCorrelatedLogs, useLogs } from './use-logs';
export { useLogStream, useLogStreamControls, type LogStreamControls } from './use-log-stream';
export { useApplyToPosting, useDiscoverPostings, usePosting, usePostings, canAutoApply } from './use-postings';
export {
  usePagePrefetch,
  usePrefetch,
  type PrefetchHandlers,
  type PrefetchableOptions,
} from './use-prefetch';
export { useCompleteOnboarding, useOnboardingStatus, useOnboardingSteps, useSubmitOnboardingStep } from './use-onboarding';
export { useDismissReview, useResolveReview, useReviewCount, useReviews } from './use-reviews';
export {
  useCreateResume,
  useDeleteResume,
  usePreviewResume,
  useResume,
  useResumeVersion,
  useResumes,
  useUpdateResume,
} from './use-resumes';
export {
  useActiveSession,
  useHydrateActiveSession,
  useSession,
  useSessions,
  useStartSession,
  useStopSession,
} from './use-sessions';
export {
  usePlugins,
  usePreferences,
  useProfile,
  useSafetyState,
  useScoringRules,
  useSettings,
  useUpdatePreferences,
  useUpdateProfile,
  useUpdateScoringRules,
  useUpdateSettings,
  type SafetyState,
} from './use-settings';
export {
  listNavigationHandlers,
  useShortcuts,
  type ShortcutHandlers,
  type UseShortcutsOptions,
} from './use-shortcuts';
export { useTheme, type ResolvedTheme, type UseThemeResult } from './use-theme';
export {
  useCreateEmailAccount,
  useDeleteEmailAccount,
  useDismissSignal,
  useEmailAccounts,
  useResolveSignal,
  useRunSync,
  useSignals,
  useTestEmailAccount,
  useTrackingStats,
} from './use-tracking';
