/**
 * Settings (`docs/UI.md` §8.11).
 *
 * **The Safety card is the reason this screen exists.** Two independent switches, both
 * defaulting closed, and submission requires `auto_apply_enabled === true` *and*
 * `dry_run === false` (golden rule #3). The card takes a `--st-review` wash while submission
 * is disabled and a `--st-success` wash when it is armed — the one place in the product where
 * a whole card carries a status tint (P7) — and the warning above the switches says in plain
 * language what turning them off, and on, actually means.
 *
 * **Autosave, optimistic, 1.2s debounce, and the only feedback is a mono timestamp.** A toast
 * per field on a screen with forty fields is forty interruptions for something the user
 * watched happen.
 *
 * Secret-bearing fields are write-only: the API returns `*_configured: boolean` and never a
 * value, so an empty box means "leave unchanged" and never "clear". The form says so rather
 * than leaving the user to discover it.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { Page } from '@/components/page';
import {
  Badge,
  Button,
  Card,
  CardBody,
  CardHeader,
  ConfirmDialog,
  EmptyState,
  Field,
  Input,
  SegmentedControl,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  Switch,
  Textarea,
} from '@/components/ui';
import { useAppActions } from '@/components/app-actions';
import {
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
} from '@/hooks';
import {
  COVER_LETTER_POLICIES,
  WORK_AUTH_STATUSES,
  type CoverLetterPolicy,
  type PreferencesUpdate,
  type SettingsUpdate,
  type WorkAuthStatus,
} from '@/lib/api/types';
import { cn, formatTime, humanize } from '@/lib/utils';
import { DENSITY_ROW_HEIGHT, useUiStore, type Density, type ThemeChoice } from '@/stores/ui';

/** Autosave debounce (§8.11). */
const SAVE_DEBOUNCE_MS = 1_200;

/** The eight sections of the rail. */
const SECTIONS = [
  'Safety',
  'Matching',
  'Providers',
  'Documents',
  'AI & tokens',
  'Knowledge',
  'Appearance',
  'Advanced',
] as const;

type Section = (typeof SECTIONS)[number];

/** Split a comma-separated field into a clean list. */
function toList(value: string): string[] {
  return value
    .split(',')
    .map((part) => part.trim())
    .filter((part) => part !== '');
}

/** The settings screen. */
export function SettingsRoute() {
  const [section, setSection] = useState<Section>('Safety');
  const [savedAt, setSavedAt] = useState<Date | null>(null);
  const [confirmReset, setConfirmReset] = useState(false);
  const [confirmRules, setConfirmRules] = useState(false);

  const settings = useSettings();
  const preferences = usePreferences();
  const profile = useProfile();
  const plugins = usePlugins({});
  const scoringRules = useScoringRules();
  const safety = useSafetyState();

  const updateSettings = useUpdateSettings();
  const updatePreferences = useUpdatePreferences();
  const updateProfile = useUpdateProfile();
  const updateScoringRules = useUpdateScoringRules();
  const { resetCache } = useAppActions();

  const settingsTimer = useRef<number | null>(null);
  const preferencesTimer = useRef<number | null>(null);

  useEffect(
    () => () => {
      if (settingsTimer.current !== null) window.clearTimeout(settingsTimer.current);
      if (preferencesTimer.current !== null) window.clearTimeout(preferencesTimer.current);
    },
    [],
  );

  /** Debounced settings write. Optimistic in the hook; the timestamp is the only feedback. */
  const saveSettings = useCallback(
    (patch: SettingsUpdate, immediate = false) => {
      if (settingsTimer.current !== null) window.clearTimeout(settingsTimer.current);
      const commit = () => {
        settingsTimer.current = null;
        updateSettings.mutate(patch, {
          onSuccess: () => {
            setSavedAt(new Date());
          },
        });
      };
      if (immediate) commit();
      else settingsTimer.current = window.setTimeout(commit, SAVE_DEBOUNCE_MS);
    },
    [updateSettings],
  );

  /** Debounced preferences write. */
  const savePreferences = useCallback(
    (patch: PreferencesUpdate, immediate = false) => {
      if (preferencesTimer.current !== null) window.clearTimeout(preferencesTimer.current);
      const commit = () => {
        preferencesTimer.current = null;
        updatePreferences.mutate(patch, {
          onSuccess: () => {
            setSavedAt(new Date());
          },
        });
      };
      if (immediate) commit();
      else preferencesTimer.current = window.setTimeout(commit, SAVE_DEBOUNCE_MS);
    },
    [updatePreferences],
  );

  const runtime = settings.data;
  const prefs = preferences.data;

  return (
    <Page
      title="Settings"
      subtitle={
        runtime === undefined
          ? 'Configuration loads from the local cache.'
          : `${runtime.environment} · ${runtime.database_backend} · data in ${runtime.data_dir}`
      }
      busy={settings.isFetching || preferences.isFetching}
    >
      <div className="flex gap-6">
        <nav aria-label="Settings sections" className="w-[180px] shrink-0">
          <ul className="flex flex-col gap-0.5">
            {SECTIONS.map((name) => (
              <li key={name}>
                <button
                  type="button"
                  onClick={() => {
                    setSection(name);
                  }}
                  aria-current={section === name ? 'page' : undefined}
                  className={cn(
                    'flex h-7 w-full items-center rounded-md px-2 text-left text-sm',
                    'transition-colors duration-[140ms] ease-out-quad hover:bg-state-hover',
                    section === name ? 'bg-accent-subtle text-primary' : 'text-secondary',
                  )}
                >
                  {name}
                </button>
              </li>
            ))}
          </ul>
        </nav>

        <div className="min-w-0 max-w-[760px] flex-1">
          <div className="mb-3 flex h-5 items-center justify-between">
            <h2 className="font-display text-md font-semibold text-primary">{section}</h2>
            <span className="font-mono text-mini tabular-nums text-muted">
              {savedAt === null ? 'Changes save automatically' : `saved ${formatTime(savedAt)}`}
            </span>
          </div>

          {section === 'Safety' && (
            <Card
              className={cn(
                safety.submissionAllowed
                  ? 'border-st-success/40 bg-st-success/[0.06]'
                  : 'border-st-review/40 bg-st-review/[0.06]',
              )}
            >
              <CardHeader
                title={
                  safety.submissionAllowed
                    ? 'Automatic submission is ARMED'
                    : 'Automatic submission is OFF'
                }
                subtitle={
                  safety.submissionAllowed
                    ? 'Runs will send real applications to real employers. Each one is recorded with a screenshot, and none of them can be taken back.'
                    : 'Nothing will be submitted until both switches below are set. Anything a run would have sent lands in the review queue instead.'
                }
              />
              <CardBody className="flex flex-col gap-4">
                <ToggleRow
                  label="Enable auto-apply"
                  token="auto_apply_enabled"
                  help="The first of two switches. With this off, the pipeline prepares everything and stops before submitting."
                  checked={runtime?.auto_apply_enabled ?? false}
                  onChange={(checked) => {
                    saveSettings({ auto_apply_enabled: checked }, true);
                  }}
                />
                <ToggleRow
                  label="Dry run"
                  token="dry_run"
                  help="The second switch, and the safer default. While it is on, the browser fills every field and then stops without pressing submit."
                  checked={runtime?.dry_run ?? true}
                  onChange={(checked) => {
                    saveSettings({ dry_run: checked }, true);
                  }}
                />

                <NumberRow
                  label="Minimum score to apply"
                  token="auto_apply_min_score"
                  value={runtime?.auto_apply_min_score}
                  min={0}
                  max={100}
                  onChange={(value) => {
                    saveSettings({ auto_apply_min_score: value });
                  }}
                />
                <NumberRow
                  label="Maximum applications per day"
                  token="max_applications_per_day"
                  value={runtime?.max_applications_per_day}
                  min={1}
                  onChange={(value) => {
                    saveSettings({ max_applications_per_day: value });
                  }}
                />
                <NumberRow
                  label="Maximum applications per run"
                  token="max_applications_per_session"
                  value={runtime?.max_applications_per_session}
                  min={1}
                  onChange={(value) => {
                    saveSettings({ max_applications_per_session: value });
                  }}
                />
                <NumberRow
                  label="Essay questions before review"
                  token="max_essay_questions_before_review"
                  help="Past this many free-text questions, the application stops and asks you."
                  value={runtime?.max_essay_questions_before_review}
                  min={0}
                  onChange={(value) => {
                    saveSettings({ max_essay_questions_before_review: value });
                  }}
                />
                <NumberRow
                  label="Minimum answer confidence"
                  token="min_answer_confidence"
                  help="Below this, an answer is never guessed at — the application waits for you."
                  value={runtime?.min_answer_confidence}
                  min={0}
                  max={1}
                  step={0.05}
                  onChange={(value) => {
                    saveSettings({ min_answer_confidence: value });
                  }}
                />
              </CardBody>
            </Card>
          )}

          {section === 'Matching' && prefs !== undefined && (
            <Card>
              <CardHeader
                title="What counts as a match"
                subtitle="These drive the deterministic score. Nothing here is inferred from your behaviour."
              />
              <CardBody className="flex flex-col gap-4">
                <NumberRow
                  label="Minimum score"
                  token="min_score"
                  help="The threshold tick drawn on every score bar."
                  value={prefs.min_score}
                  min={0}
                  max={100}
                  onChange={(value) => {
                    savePreferences({ min_score: value });
                  }}
                />
                <NumberRow
                  label="Minimum salary"
                  token="min_salary"
                  value={prefs.min_salary ?? undefined}
                  min={0}
                  onChange={(value) => {
                    savePreferences({ min_salary: value });
                  }}
                />
                <ToggleRow
                  label="Remote only"
                  token="remote_only"
                  checked={prefs.remote_only}
                  onChange={(checked) => {
                    savePreferences({ remote_only: checked }, );
                  }}
                />
                <ToggleRow
                  label="Skip roles that need sponsorship"
                  token="require_no_sponsorship"
                  checked={prefs.require_no_sponsorship}
                  onChange={(checked) => {
                    savePreferences({ require_no_sponsorship: checked });
                  }}
                />
                <ToggleRow
                  label="Exclude defense contractors"
                  token="exclude_defense"
                  checked={prefs.exclude_defense}
                  onChange={(checked) => {
                    savePreferences({ exclude_defense: checked });
                  }}
                />
                <ListRow
                  label="Preferred keywords"
                  token="preferred_keywords"
                  value={prefs.preferred_keywords}
                  onChange={(value) => {
                    savePreferences({ preferred_keywords: value });
                  }}
                />
                <ListRow
                  label="Preferred locations"
                  token="preferred_locations"
                  value={prefs.preferred_locations}
                  onChange={(value) => {
                    savePreferences({ preferred_locations: value });
                  }}
                />
                <ListRow
                  label="Blocked companies"
                  token="blocked_companies"
                  help="A hard negative — a blocked company zeroes the score outright."
                  value={prefs.blocked_companies}
                  onChange={(value) => {
                    savePreferences({ blocked_companies: value });
                  }}
                />
                <ListRow
                  label="Blocked industries"
                  token="blocked_industries"
                  value={prefs.blocked_industries}
                  onChange={(value) => {
                    savePreferences({ blocked_industries: value });
                  }}
                />
                <Field label="Cover letter policy" htmlFor="cover-letter-policy">
                  <Select
                    value={prefs.cover_letter_policy}
                    onValueChange={(value) => {
                      savePreferences({ cover_letter_policy: value as CoverLetterPolicy }, true);
                    }}
                  >
                    <SelectTrigger id="cover-letter-policy" aria-label="Cover letter policy">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {COVER_LETTER_POLICIES.map((policy) => (
                        <SelectItem key={policy} value={policy}>
                          {humanize(policy)}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </Field>

                {profile.data?.profile != null && (
                  <Field
                    label="Work authorization"
                    help="Never inferred. It is asked once and used verbatim."
                    htmlFor="work-auth"
                  >
                    <Select
                      value={profile.data.profile.work_authorization}
                      onValueChange={(value) => {
                        updateProfile.mutate(
                          { work_authorization: value as WorkAuthStatus },
                          {
                            onSuccess: () => {
                              setSavedAt(new Date());
                            },
                          },
                        );
                      }}
                    >
                      <SelectTrigger id="work-auth" aria-label="Work authorization">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        {WORK_AUTH_STATUSES.map((status) => (
                          <SelectItem key={status} value={status}>
                            {humanize(status)}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </Field>
                )}
              </CardBody>
            </Card>
          )}

          {section === 'Providers' && (
            <Card>
              <CardHeader
                title="Plugins"
                subtitle="Registered at start-up. Changing what is installed needs a restart."
              />
              <CardBody>
                {(plugins.data?.items ?? []).length === 0 ? (
                  <EmptyState
                    title="No plugins registered"
                    description="Providers, models, templates, parsers and analyzers all register through one registry."
                  />
                ) : (
                  <ul className="flex flex-col">
                    {(plugins.data?.items ?? []).map((plugin) => (
                      <li
                        key={`${plugin.kind}:${plugin.name}`}
                        className="flex items-start gap-3 border-b border-state-divider py-2 last:border-b-0"
                      >
                        <span className="min-w-0 flex-1">
                          <span className="block truncate text-sm text-primary">
                            {plugin.display_name}
                          </span>
                          <span className="block truncate text-mini text-muted">
                            {plugin.description}
                          </span>
                          {plugin.capabilities.length > 0 && (
                            <span className="mt-1 flex flex-wrap gap-1">
                              {plugin.capabilities.map((capability) => (
                                <span key={capability} className="chip-mono">
                                  {capability}
                                </span>
                              ))}
                            </span>
                          )}
                        </span>
                        <span className="shrink-0 font-mono text-micro tracking-normal text-muted">
                          {plugin.kind} v{plugin.version}
                        </span>
                        <Badge
                          tone={{
                            family: plugin.enabled ? 'success' : 'dim',
                            label: plugin.enabled ? 'Enabled' : 'Disabled',
                            dot: plugin.enabled ? 'filled' : 'dashed',
                            animated: false,
                            color: plugin.enabled ? 'var(--st-success)' : 'var(--st-dim)',
                            text: plugin.enabled ? 'var(--st-success)' : 'var(--fg-muted)',
                            wash: 0.12,
                            solid: false,
                          }}
                        />
                      </li>
                    ))}
                  </ul>
                )}
              </CardBody>
            </Card>
          )}

          {section === 'Documents' && (
            <Card>
              <CardHeader
                title="Documents"
                subtitle="How résumés are rendered, and what happens to the file afterwards."
              />
              <CardBody className="flex flex-col gap-4">
                <TextRow
                  label="Résumé template"
                  token="resume_template"
                  value={runtime?.resume_template ?? ''}
                  onChange={(value) => {
                    saveSettings({ resume_template: value });
                  }}
                />
                <NumberRow
                  label="Maximum pages"
                  token="resume_max_pages"
                  value={runtime?.resume_max_pages}
                  min={1}
                  max={4}
                  onChange={(value) => {
                    saveSettings({ resume_max_pages: value });
                  }}
                />
                <Field label="PDF engine" htmlFor="pdf-engine">
                  <Select
                    value={runtime?.pdf_engine ?? 'html'}
                    onValueChange={(value) => {
                      saveSettings(
                        { pdf_engine: value as 'latex' | 'docx' | 'html' },
                        true,
                      );
                    }}
                  >
                    <SelectTrigger id="pdf-engine" aria-label="PDF engine">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="latex">LaTeX</SelectItem>
                      <SelectItem value="docx">DOCX</SelectItem>
                      <SelectItem value="html">HTML</SelectItem>
                    </SelectContent>
                  </Select>
                </Field>
                <ToggleRow
                  label="Delete the tailored file after submission"
                  token="delete_temp_resume_after_submit"
                  help="The structured version is kept forever either way — the rendered file is the disposable part."
                  checked={runtime?.delete_temp_resume_after_submit ?? true}
                  onChange={(checked) => {
                    saveSettings({ delete_temp_resume_after_submit: checked }, true);
                  }}
                />
              </CardBody>
            </Card>
          )}

          {section === 'AI & tokens' && (
            <Card>
              <CardHeader
                title="Models and budget"
                subtitle="ApplicantOS runs end to end with no API keys at all: choose the null provider and the hashing embedder."
              />
              <CardBody className="flex flex-col gap-4">
                <Field label="Language model provider" htmlFor="llm-provider">
                  <Select
                    value={runtime?.llm_provider ?? 'null'}
                    onValueChange={(value) => {
                      saveSettings(
                        { llm_provider: value as 'anthropic' | 'openai' | 'local' | 'null' },
                        true,
                      );
                    }}
                  >
                    <SelectTrigger id="llm-provider" aria-label="Language model provider">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="anthropic">Anthropic</SelectItem>
                      <SelectItem value="openai">OpenAI</SelectItem>
                      <SelectItem value="local">Local</SelectItem>
                      <SelectItem value="null">None — deterministic fallbacks</SelectItem>
                    </SelectContent>
                  </Select>
                </Field>

                <NumberRow
                  label="Daily token budget"
                  token="llm_daily_token_budget"
                  value={runtime?.llm_daily_token_budget}
                  min={0}
                  onChange={(value) => {
                    saveSettings({ llm_daily_token_budget: value });
                  }}
                />

                <SecretRow
                  label="Anthropic API key"
                  token="anthropic_api_key"
                  configured={runtime?.anthropic_configured ?? false}
                  onChange={(value) => {
                    saveSettings({ anthropic_api_key: value }, true);
                  }}
                />
                <SecretRow
                  label="OpenAI API key"
                  token="openai_api_key"
                  configured={runtime?.openai_configured ?? false}
                  onChange={(value) => {
                    saveSettings({ openai_api_key: value }, true);
                  }}
                />
                <SecretRow
                  label="GitHub token"
                  token="github_token"
                  configured={runtime?.github_configured ?? false}
                  onChange={(value) => {
                    saveSettings({ github_token: value }, true);
                  }}
                />
              </CardBody>
            </Card>
          )}

          {section === 'Knowledge' && (
            <Card>
              <CardHeader
                title="Indexing"
                subtitle="How your sources are read. Re-indexing is cheap because a source whose fingerprint has not moved is skipped."
              />
              <CardBody className="flex flex-col gap-4">
                <ToggleRow
                  label="Index automatically"
                  token="knowledge_autoindex"
                  checked={runtime?.knowledge_autoindex ?? true}
                  onChange={(checked) => {
                    saveSettings({ knowledge_autoindex: checked }, true);
                  }}
                />
                <NumberRow
                  label="Re-index every (minutes)"
                  token="knowledge_reindex_interval_minutes"
                  value={runtime?.knowledge_reindex_interval_minutes}
                  min={5}
                  onChange={(value) => {
                    saveSettings({ knowledge_reindex_interval_minutes: value });
                  }}
                />
                <NumberRow
                  label="Chunk size (tokens)"
                  token="knowledge_chunk_tokens"
                  value={runtime?.knowledge_chunk_tokens}
                  min={64}
                  onChange={(value) => {
                    saveSettings({ knowledge_chunk_tokens: value });
                  }}
                />
                <NumberRow
                  label="Chunk overlap (tokens)"
                  token="knowledge_chunk_overlap"
                  value={runtime?.knowledge_chunk_overlap}
                  min={0}
                  onChange={(value) => {
                    saveSettings({ knowledge_chunk_overlap: value });
                  }}
                />
                <NumberRow
                  label="GitHub repositories to read"
                  token="github_max_repos"
                  value={runtime?.github_max_repos}
                  min={1}
                  onChange={(value) => {
                    saveSettings({ github_max_repos: value });
                  }}
                />
                <ToggleRow
                  label="Include forks"
                  token="github_include_forks"
                  checked={runtime?.github_include_forks ?? false}
                  onChange={(checked) => {
                    saveSettings({ github_include_forks: checked }, true);
                  }}
                />
              </CardBody>
            </Card>
          )}

          {section === 'Appearance' && <AppearanceSection />}

          {section === 'Advanced' && (
            <div className="flex flex-col gap-3">
              <Card>
                <CardHeader
                  title="Scoring rules"
                  subtitle="The deterministic rule pack. Scoring stays pure: same posting, same preferences, same score."
                />
                <CardBody>
                  {(scoringRules.data?.items ?? []).length === 0 ? (
                    <EmptyState title="No rules loaded" />
                  ) : (
                    <ul className="flex flex-col">
                      {(scoringRules.data?.items ?? []).map((rule) => (
                        <li
                          key={rule.key}
                          className="flex items-center gap-3 border-b border-state-divider py-1.5 last:border-b-0"
                        >
                          <span className="min-w-0 flex-1">
                            <span className="block truncate text-sm text-primary">
                              {rule.label}
                            </span>
                            <span className="block truncate text-mini text-muted">
                              {rule.description ?? rule.category}
                            </span>
                          </span>
                          {rule.hard_negative && (
                            <span className="shrink-0 text-mini text-st-danger">hard negative</span>
                          )}
                          <span className="w-[64px] shrink-0 text-right font-mono text-mini tabular-nums text-primary">
                            {rule.points.toFixed(1)}
                          </span>
                          <span className="w-[56px] shrink-0 text-right font-mono text-mini tabular-nums text-muted">
                            ×{rule.weight.toFixed(2)}
                          </span>
                          <Switch
                            checked={rule.enabled}
                            aria-label={`Enable ${rule.label}`}
                            onCheckedChange={(checked) => {
                              const rules = (scoringRules.data?.items ?? []).map((entry) =>
                                entry.key === rule.key ? { ...entry, enabled: checked } : entry,
                              );
                              updateScoringRules.mutate(
                                { rules },
                                {
                                  onSuccess: () => {
                                    setSavedAt(new Date());
                                  },
                                },
                              );
                            }}
                          />
                        </li>
                      ))}
                    </ul>
                  )}
                  <Button
                    variant="secondary"
                    className="mt-3"
                    onClick={() => {
                      setConfirmRules(true);
                    }}
                  >
                    Reset the rule pack
                  </Button>
                </CardBody>
              </Card>

              <Card>
                <CardHeader
                  title="Local cache"
                  subtitle="Everything this window shows instantly is read from disk. Clearing it costs one slow launch and nothing else."
                />
                <CardBody className="flex flex-col gap-3">
                  <p className="text-sm text-secondary">
                    Your applications, postings and knowledge live in the backend&apos;s database.
                    This clears only the renderer&apos;s copy.
                  </p>
                  <Button
                    variant="danger"
                    className="self-start"
                    onClick={() => {
                      setConfirmReset(true);
                    }}
                  >
                    Reset the local cache
                  </Button>
                </CardBody>
              </Card>

              {runtime !== undefined && (
                <Card>
                  <CardHeader title="Runtime" subtitle="Read-only, for support." />
                  <CardBody>
                    <dl className="grid grid-cols-2 gap-x-6 gap-y-1 text-sm">
                      <Row label="Environment" value={runtime.environment} />
                      <Row label="Database" value={runtime.database_backend} />
                      <Row label="Cache backend" value={runtime.cache_backend} />
                      <Row label="Vector store" value={runtime.vector_store} />
                      <Row label="Embeddings" value={runtime.embedding_provider} />
                      <Row label="Storage" value={runtime.storage_backend} />
                      <Row label="API" value={`${runtime.api_host}:${String(runtime.api_port)}`} />
                      <Row label="Data directory" value={runtime.data_dir} />
                    </dl>
                  </CardBody>
                </Card>
              )}
            </div>
          )}
        </div>
      </div>

      <ConfirmDialog
        open={confirmReset}
        onOpenChange={setConfirmReset}
        title="Reset the local cache?"
        description="Every persisted query is dropped and refetched. Nothing on the server is touched, and no application is affected."
        confirmLabel="Reset the Cache"
        onConfirm={() => {
          setConfirmReset(false);
          resetCache();
        }}
      />

      <ConfirmDialog
        open={confirmRules}
        onOpenChange={setConfirmRules}
        title="Reset the scoring rules?"
        description="Every rule returns to the pack shipped with this build. Scores already recorded are unchanged; future ones use the defaults."
        confirmLabel="Reset the Rules"
        loading={updateScoringRules.isPending}
        onConfirm={() => {
          setConfirmRules(false);
          updateScoringRules.mutate(
            { reset_to_defaults: true },
            {
              onSuccess: () => {
                setSavedAt(new Date());
              },
            },
          );
        }}
      />
    </Page>
  );
}

/** Theme, density and the honest note about motion. */
function AppearanceSection() {
  const theme = useUiStore((state) => state.theme);
  const setTheme = useUiStore((state) => state.setTheme);
  const density = useUiStore((state) => state.density);
  const setDensity = useUiStore((state) => state.setDensity);

  return (
    <Card>
      <CardHeader title="Appearance" subtitle="Applies immediately and persists per machine." />
      <CardBody className="flex flex-col gap-4">
        <Field label="Theme" help="System follows your operating system as it changes.">
          <SegmentedControl<ThemeChoice>
            label="Theme"
            value={theme}
            onValueChange={setTheme}
            options={[
              { value: 'system', label: 'System' },
              { value: 'dark', label: 'Dark' },
              { value: 'light', label: 'Light' },
            ]}
          />
        </Field>

        <Field
          label="Row density"
          help={`Table rows are ${String(DENSITY_ROW_HEIGHT[density])}px tall. Two-line rows keep a 44px floor whatever this is set to.`}
        >
          <SegmentedControl<Density>
            label="Row density"
            value={density}
            onValueChange={setDensity}
            options={[
              { value: 'compact', label: 'Compact' },
              { value: 'default', label: 'Default' },
              { value: 'comfortable', label: 'Comfortable' },
            ]}
          />
        </Field>

        <p className="text-sm text-secondary">
          Motion follows your operating system&apos;s reduce-motion setting. When it is on,
          transforms are dropped and only opacity and colour changes remain — enough to see that
          something changed, without the movement.
        </p>
      </CardBody>
    </Card>
  );
}

/** A labelled switch with its configuration key shown in mono. */
function ToggleRow({
  label,
  token,
  help,
  checked,
  onChange,
}: {
  label: string;
  token: string;
  help?: string;
  checked: boolean;
  onChange: (checked: boolean) => void;
}) {
  return (
    <div className="flex items-start justify-between gap-4">
      <span className="min-w-0">
        <span className="block text-sm text-primary">{label}</span>
        {help !== undefined && <span className="block text-mini text-muted">{help}</span>}
      </span>
      <span className="flex shrink-0 items-center gap-3">
        <span className="font-mono text-micro tracking-normal text-muted">{token}</span>
        <Switch checked={checked} onCheckedChange={onChange} aria-label={label} />
      </span>
    </div>
  );
}

/** A numeric field with its configuration key shown in mono. */
function NumberRow({
  label,
  token,
  help,
  value,
  min,
  max,
  step,
  onChange,
}: {
  label: string;
  token: string;
  help?: string;
  value: number | undefined;
  min?: number;
  max?: number;
  step?: number;
  onChange: (value: number) => void;
}) {
  const [draft, setDraft] = useState(value === undefined ? '' : String(value));
  useEffect(() => {
    setDraft(value === undefined ? '' : String(value));
  }, [value]);

  return (
    <div className="flex items-center justify-between gap-4">
      <span className="min-w-0">
        <span className="block text-sm text-primary">{label}</span>
        {help !== undefined && <span className="block text-mini text-muted">{help}</span>}
      </span>
      <span className="flex shrink-0 items-center gap-3">
        <span className="font-mono text-micro tracking-normal text-muted">{token}</span>
        <Input
          type="number"
          mono
          size="sm"
          className="w-24"
          value={draft}
          aria-label={label}
          {...(min === undefined ? {} : { min })}
          {...(max === undefined ? {} : { max })}
          {...(step === undefined ? {} : { step })}
          onChange={(event) => {
            setDraft(event.target.value);
            const parsed = Number.parseFloat(event.target.value);
            if (Number.isFinite(parsed)) onChange(parsed);
          }}
        />
      </span>
    </div>
  );
}

/** A short text field with its configuration key shown in mono. */
function TextRow({
  label,
  token,
  value,
  onChange,
}: {
  label: string;
  token: string;
  value: string;
  onChange: (value: string) => void;
}) {
  const [draft, setDraft] = useState(value);
  useEffect(() => {
    setDraft(value);
  }, [value]);

  return (
    <div className="flex items-center justify-between gap-4">
      <span className="block text-sm text-primary">{label}</span>
      <span className="flex shrink-0 items-center gap-3">
        <span className="font-mono text-micro tracking-normal text-muted">{token}</span>
        <Input
          size="sm"
          mono
          className="w-48"
          value={draft}
          aria-label={label}
          onChange={(event) => {
            setDraft(event.target.value);
            onChange(event.target.value);
          }}
        />
      </span>
    </div>
  );
}

/** A comma-separated list field. */
function ListRow({
  label,
  token,
  help,
  value,
  onChange,
}: {
  label: string;
  token: string;
  help?: string;
  value: readonly string[];
  onChange: (value: string[]) => void;
}) {
  const initial = useMemo(() => value.join(', '), [value]);
  const [draft, setDraft] = useState(initial);
  useEffect(() => {
    setDraft(initial);
  }, [initial]);

  return (
    <Field
      label={label}
      {...(help === undefined ? {} : { help })}
      htmlFor={`list-${token}`}
    >
      <Textarea
        id={`list-${token}`}
        autoGrow
        rows={2}
        value={draft}
        placeholder="Comma separated"
        onChange={(event) => {
          setDraft(event.target.value);
          onChange(toList(event.target.value));
        }}
      />
    </Field>
  );
}

/**
 * A write-only secret.
 *
 * The API never returns a value — only `*_configured` — so the box is always empty and an
 * empty box always means "leave unchanged". Saying that out loud is the difference between a
 * field that looks broken and one that is obviously safe.
 */
function SecretRow({
  label,
  token,
  configured,
  onChange,
}: {
  label: string;
  token: string;
  configured: boolean;
  onChange: (value: string) => void;
}) {
  const [draft, setDraft] = useState('');

  return (
    <Field
      label={label}
      help={
        configured
          ? 'A key is stored. Leave this blank to keep it; type a new one to replace it.'
          : 'No key stored. Without one, this provider is unavailable and the pipeline falls back.'
      }
      htmlFor={`secret-${token}`}
    >
      <div className="flex items-center gap-2">
        <Input
          id={`secret-${token}`}
          type="password"
          mono
          autoComplete="off"
          value={draft}
          placeholder={configured ? '•••••••• stored in the backend' : 'not configured'}
          onChange={(event) => {
            setDraft(event.target.value);
          }}
        />
        <Button
          variant="secondary"
          disabled={draft === ''}
          onClick={() => {
            onChange(draft);
            setDraft('');
          }}
        >
          Save
        </Button>
      </div>
    </Field>
  );
}

/** One read-only runtime row. */
function Row({ label, value }: { label: string; value: string }) {
  return (
    <>
      <dt className="text-secondary">{label}</dt>
      <dd className="truncate font-mono text-mini text-primary" title={value}>
        {value}
      </dd>
    </>
  );
}
