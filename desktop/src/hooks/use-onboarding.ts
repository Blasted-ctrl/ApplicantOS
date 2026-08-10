/**
 * The onboarding wizard.
 *
 * Two properties set this family apart from everything else in the app, and both are in the
 * §10.3 table: `staleTime: 0` and **not persisted**. A wizard is the one place where stale
 * data would ask the user to redo work they already did, and a persisted step state restored
 * from a previous launch is exactly that failure. It is cheap to refetch — there is one user
 * and one wizard — and expensive to get wrong.
 *
 * Each step POST returns the *new status*, so the response is written straight into the cache
 * rather than triggering a refetch: the wizard advances on the same frame as the submit.
 *
 * `POST /onboarding/complete` is not optimistic. It enqueues the first knowledge index, which
 * is real background work; the wizard closes when the server says the user is onboarded, not
 * when the button was pressed.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import * as api from '@/lib/api/endpoints';
import type { OnboardingStatus } from '@/lib/api/types';
import { notifyError, notifyIfNotStarted } from '@/lib/notify';
import { qk } from '@/lib/query/keys';
import { onboardingStatusOptions, onboardingStepsOptions } from '@/lib/query/options';

/** `GET /onboarding/status` — progress, the current step, and each step's completion. */
export function useOnboardingStatus() {
  return useQuery(onboardingStatusOptions());
}

/** `GET /onboarding/steps` — the step definitions, detailed enough to render blind. */
export function useOnboardingSteps() {
  return useQuery(onboardingStepsOptions());
}

/**
 * `POST /onboarding/steps/{step}` — submit one screen's answers.
 *
 * The values are keyed by `OnboardingField.name`, and the server validates them against the
 * same definition it handed out, so the client never has to know what a step means.
 */
export function useSubmitOnboardingStep() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({ step, values }: { step: string; values: Record<string, unknown> }) =>
      api.submitOnboardingStep(step, values),

    onSuccess: (status: OnboardingStatus) => {
      queryClient.setQueryData(qk.onboardingStatus(), status);
      // Answers land on the profile and preferences, so both are stale the moment a step
      // is accepted.
      void queryClient.invalidateQueries({ queryKey: qk.settingsAll(), refetchType: 'active' });
    },

    onError: (error) => {
      notifyError('Could not save this step', error);
    },
  });
}

/**
 * `POST /onboarding/complete` — **not optimistic**.
 *
 * Answered 202: the first index is enqueued and reports as `knowledge.index_*` frames. The
 * whole app's data is about to change, so every family is invalidated rather than guessed at.
 */
export function useCompleteOnboarding() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: () => api.completeOnboarding(),
    onSuccess: (response) => {
      // The exact path that produced the reported symptom: the wizard ended with "3
      // knowledge source(s) queued for indexing" while nothing indexed them, and the user
      // was left with an empty graph and no reason to doubt it. A résumé cannot be
      // generated from knowledge that was never built, so this is the last moment where
      // saying so is cheap.
      notifyIfNotStarted(response, 'Indexing your sources');
      void queryClient.invalidateQueries({ queryKey: qk.onboarding() });
      void queryClient.invalidateQueries({ queryKey: qk.settingsAll(), refetchType: 'active' });
      void queryClient.invalidateQueries({ queryKey: qk.knowledge(), refetchType: 'active' });
    },
    onError: (error) => {
      notifyError('Could not finish setting up', error);
    },
  });
}
