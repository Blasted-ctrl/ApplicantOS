/**
 * Runtime settings, the user's profile, preferences, plugins, and the scoring rules.
 *
 * The kill switch lives here, and it is the one mutation in the product that deserves an
 * explicit note. `auto_apply_enabled` and `dry_run` are two independent switches that both
 * default to the safe position, and submission requires `auto_apply_enabled === true` **and**
 * `dry_run === false` (golden rule #3). {@link useSafetyState} reads both plus the server's
 * own `is_submission_allowed`, so the titlebar chip and any confirmation dialog agree about
 * what is armed — a UI that computed the conjunction itself would be a second implementation
 * of a safety check.
 *
 * Settings mutations are optimistic. They are form fields with immediate visual consequences
 * — a toggle that lags feels broken — and a failure restores the previous object, which for a
 * settings screen is the whole state.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import * as api from '@/lib/api/endpoints';
import type {
  PluginFilters,
  PreferencesRead,
  PreferencesUpdate,
  ProfileUpdate,
  ScoringRulesUpdate,
  SettingsRead,
  SettingsUpdate,
} from '@/lib/api/types';
import { notifyError } from '@/lib/notify';
import { qk } from '@/lib/query/keys';
import {
  pluginsOptions,
  preferencesOptions,
  profileOptions,
  scoringRulesOptions,
  settingsOptions,
} from '@/lib/query/options';

/** `GET /settings` — the redacted projection. Secrets appear only as `*_configured`. */
export function useSettings() {
  return useQuery(settingsOptions());
}

/** `GET /profile`. */
export function useProfile() {
  return useQuery(profileOptions());
}

/** `GET /profile/preferences`. */
export function usePreferences() {
  return useQuery(preferencesOptions());
}

/** `GET /settings/plugins`. */
export function usePlugins(filters: PluginFilters = {}) {
  return useQuery(pluginsOptions(filters));
}

/** `GET /settings/scoring-rules`. */
export function useScoringRules() {
  return useQuery(scoringRulesOptions());
}

/** The two halves of the kill switch, plus the server's own verdict on them. */
export interface SafetyState {
  autoApplyEnabled: boolean;
  dryRun: boolean;
  /** The server's `auto_apply_enabled && !dry_run`. The only value worth trusting. */
  submissionAllowed: boolean;
  /** Copy for the titlebar chip, or `null` when both switches are armed. */
  warning: string | null;
}

/**
 * The safety envelope, resolved for display.
 *
 * Defaults to the *safe* reading while settings are loading: an app that has not yet been
 * told the switches are armed must not imply that they are. `docs/UI.md` §P7 — safety states
 * are louder than success states, and that includes the unknown state.
 */
export function useSafetyState(): SafetyState {
  const { data } = useSettings();
  const autoApplyEnabled = data?.auto_apply_enabled ?? false;
  const dryRun = data?.dry_run ?? true;
  const submissionAllowed = data?.is_submission_allowed ?? false;

  const warning = dryRun ? 'Dry run' : autoApplyEnabled ? null : 'Auto-apply off';

  return { autoApplyEnabled, dryRun, submissionAllowed, warning };
}

/** `PUT /settings` — optimistic, with rollback. */
export function useUpdateSettings() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (body: SettingsUpdate) => api.updateSettings(body),

    onMutate: async (body) => {
      await queryClient.cancelQueries({ queryKey: qk.settings() });
      const previous = queryClient.getQueryData<SettingsRead>(qk.settings());
      queryClient.setQueryData<SettingsRead>(qk.settings(), (old) => {
        if (old === undefined) return old;
        const next: SettingsRead = { ...old };
        for (const [key, value] of Object.entries(body)) {
          // Write-only fields (`*_api_key`, `*_token`) have no counterpart in the read
          // projection; copying them in would invent a field the server never returns.
          if (value !== undefined && value !== null && key in next) {
            (next as unknown as Record<string, unknown>)[key] = value;
          }
        }
        // The kill switch is derived, so it is recomputed rather than left stale — otherwise
        // the titlebar chip would disagree with the toggle the user just flipped.
        next.is_submission_allowed = next.auto_apply_enabled && !next.dry_run;
        return next;
      });
      return { previous };
    },

    onError: (error, _body, context) => {
      queryClient.setQueryData(qk.settings(), context?.previous);
      notifyError('Could not save settings', error);
    },

    onSuccess: (settings) => {
      queryClient.setQueryData(qk.settings(), settings);
    },
  });
}

/** `PUT /profile` — optimistic on the nested profile object. */
export function useUpdateProfile() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (body: ProfileUpdate) => api.updateProfile(body),
    onSuccess: (user) => {
      queryClient.setQueryData(qk.profile(), user);
      queryClient.setQueryData(qk.preferences(), user.preferences);
    },
    onError: (error) => {
      notifyError('Could not save your profile', error);
    },
  });
}

/** `PUT /profile/preferences` — optimistic, with rollback. */
export function useUpdatePreferences() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (body: PreferencesUpdate) => api.updatePreferences(body),

    onMutate: async (body) => {
      await queryClient.cancelQueries({ queryKey: qk.preferences() });
      const previous = queryClient.getQueryData<PreferencesRead>(qk.preferences());
      queryClient.setQueryData<PreferencesRead>(qk.preferences(), (old) => {
        if (old === undefined) return old;
        const next: PreferencesRead = { ...old };
        for (const [key, value] of Object.entries(body)) {
          if (value !== undefined && value !== null && key in next) {
            (next as unknown as Record<string, unknown>)[key] = value;
          }
        }
        return next;
      });
      return { previous };
    },

    onError: (error, _body, context) => {
      queryClient.setQueryData(qk.preferences(), context?.previous);
      notifyError('Could not save your preferences', error);
    },

    onSettled: () => {
      // `min_score` changes what every ScoreBar's threshold tick points at, and the profile
      // response nests preferences, so both are refreshed together.
      void queryClient.invalidateQueries({ queryKey: qk.profile(), refetchType: 'active' });
    },
  });
}

/**
 * `PUT /settings/scoring-rules` — replaces the whole pack, or resets it to the defaults.
 *
 * Not optimistic: `score_rules()` is pure and deterministic server-side, and the response is
 * the authoritative pack including any rule the server normalised.
 */
export function useUpdateScoringRules() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (body: ScoringRulesUpdate) => api.updateScoringRules(body),
    onSuccess: (page) => {
      queryClient.setQueryData(qk.scoringRules(), page);
    },
    onError: (error) => {
      notifyError('Could not save the scoring rules', error);
    },
  });
}
