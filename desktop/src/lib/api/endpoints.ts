/**
 * One function per endpoint in `docs/CONTRACTS.md` §14 and §17.7.
 *
 * This layer exists so that no path string and no query-parameter name appears anywhere else
 * in the renderer. A route or a hook that assembled `/api/v1/applications?status=…` itself
 * would be a second, silent copy of the contract; when the backend renamed a parameter, the
 * type checker would have nothing to say about it.
 *
 * Every function here is a plain `Promise`-returning call with an optional `AbortSignal`, so
 * it drops straight into a TanStack Query `queryFn` (which supplies the signal) and into a
 * route loader alike. None of them touch the cache — that is `lib/query/`'s job, and keeping
 * the two apart is what lets a loader and a component share one `queryOptions` object.
 */

import { absoluteUrl, del, get, getBlob, patch, post, postForm, put, request } from './client';
import type {
  AnalyticsOverview,
  ApplicationDetail,
  ApplicationFilters,
  ApplicationRead,
  ApplicationUpdate,
  ArtifactRead,
  DocumentKind,
  FileRead,
  DiscoverRequest,
  EmailAccountCreate,
  EmailAccountRead,
  EntityFilters,
  EntityRead,
  FactFilters,
  FactRead,
  FactUpdate,
  FunnelStage,
  GraphFilters,
  GraphView,
  HealthReport,
  InsightItem,
  KnowledgeSearchResult,
  KnowledgeStats,
  LogEntryRead,
  LogFilters,
  OkResponse,
  OnboardingStatus,
  OnboardingStep,
  Page,
  PageParams,
  PluginFilters,
  PluginInfo,
  PostingFilters,
  PostingRead,
  PreferencesRead,
  PreferencesUpdate,
  ProfileUpdate,
  ReadinessReport,
  ResumeCreate,
  ResumePreviewRequest,
  ResumeRead,
  ResumeUpdate,
  ResumeVersionRead,
  ReviewDismiss,
  ReviewItem,
  ReviewResolve,
  ScoreRuleSchema,
  ScoringRulesUpdate,
  SessionDetail,
  SessionRead,
  SessionStartRequest,
  SettingsRead,
  SettingsUpdate,
  SignalFilters,
  SignalResolve,
  SourceCreate,
  SourceRead,
  StatusSignalRead,
  SyncRequest,
  TimeseriesPoint,
  TrackingStats,
  UserRead,
  Uuid,
} from './types';
import { API_PREFIX } from './types';

/** Filters as accepted by `request()`. A widening cast, not a change of shape. */
type Query = Record<string, string | number | boolean | null | undefined | readonly string[]>;

/**
 * Widen a typed filter object to the client's query shape.
 *
 * The filter interfaces are deliberately narrow so a typo is a compile error; the client
 * takes the general shape so it can serialise anything. This is the one place the two meet.
 */
function q(filters: object | undefined): Query {
  return (filters ?? {}) as Query;
}

// ══════════════════════════════════════════════════════════════════════════════════════
// Health — the three routes outside /api/v1
// ══════════════════════════════════════════════════════════════════════════════════════

/**
 * `GET /health`. Liveness plus the backend version used as half the cache buster.
 *
 * Called with a short timeout at boot: if the backend is not up, the answer is needed
 * quickly so the shell can show its error state, not thirty seconds later.
 */
export function getHealth(signal?: AbortSignal): Promise<HealthReport> {
  return request<HealthReport>('/health', {
    timeoutMs: 5_000,
    ...(signal === undefined ? {} : { signal }),
  });
}

/** `GET /ready`. Per-dependency readiness, for the backend-unavailable screen. */
export function getReadiness(signal?: AbortSignal): Promise<ReadinessReport> {
  return request<ReadinessReport>('/ready', {
    timeoutMs: 5_000,
    ...(signal === undefined ? {} : { signal }),
  });
}

// ══════════════════════════════════════════════════════════════════════════════════════
// Files
// ══════════════════════════════════════════════════════════════════════════════════════

/**
 * `POST /files` — upload bytes and get back the catalogue entry.
 *
 * The only multipart call in the client. The returned `id` is what every other endpoint
 * references a blob by: `master_resume.file_id` on the wizard, a resume knowledge source,
 * an attachment on a review.
 */
export function uploadFile(
  file: File,
  kind: DocumentKind = 'other',
  signal?: AbortSignal,
): Promise<FileRead> {
  const form = new FormData();
  form.append('file', file);
  form.append('kind', kind);
  return postForm<FileRead>('/files', form, signal);
}

/** `GET /files` — this user's uploads, newest first. */
export function listFiles(
  kind?: DocumentKind,
  options?: { limit?: number; offset?: number },
  signal?: AbortSignal,
): Promise<Page<FileRead>> {
  return get<Page<FileRead>>(
    '/files',
    { ...(kind === undefined ? {} : { kind }), ...options },
    signal,
  );
}

/** `DELETE /files/{id}` — soft-delete one catalogue entry. The bytes are reclaimed later. */
export function deleteFile(fileId: Uuid, signal?: AbortSignal): Promise<OkResponse> {
  return del<OkResponse>(`/files/${fileId}`, undefined, signal);
}

/** Absolute URL for a file's bytes — for `<a download>` and `<img src>`. */
export function fileContentUrl(fileId: Uuid): Promise<string> {
  return absoluteUrl(`${API_PREFIX}/files/${fileId}/content`);
}

// ══════════════════════════════════════════════════════════════════════════════════════
// Onboarding
// ══════════════════════════════════════════════════════════════════════════════════════

/** `GET /onboarding/status`. */
export function getOnboardingStatus(signal?: AbortSignal): Promise<OnboardingStatus> {
  return get<OnboardingStatus>('/onboarding/status', undefined, signal);
}

/** `GET /onboarding/steps`. Note: a bare array, not a `Page`. */
export function getOnboardingSteps(signal?: AbortSignal): Promise<OnboardingStep[]> {
  return get<OnboardingStep[]>('/onboarding/steps', undefined, signal);
}

/**
 * `POST /onboarding/steps/{step}` — submit one screen's answers.
 *
 * The body is the step's field values keyed by `OnboardingField.name`; the server validates
 * against the same step definition it handed out, so the client renders blind.
 */
export function submitOnboardingStep(
  step: string,
  values: Record<string, unknown>,
  signal?: AbortSignal,
): Promise<OnboardingStatus> {
  return post<OnboardingStatus>(
    `/onboarding/steps/${encodeURIComponent(step)}`,
    values,
    undefined,
    signal,
  );
}

/**
 * `POST /onboarding/complete` — finish the wizard and enqueue the first index.
 *
 * Not optimistic (`docs/UI.md` §10.9): it starts real background work.
 */
export function completeOnboarding(signal?: AbortSignal): Promise<OkResponse> {
  return post<OkResponse>('/onboarding/complete', undefined, undefined, signal);
}

// ══════════════════════════════════════════════════════════════════════════════════════
// Profile and preferences
// ══════════════════════════════════════════════════════════════════════════════════════

/** `GET /profile` — the user with preferences and profile nested. */
export function getProfile(signal?: AbortSignal): Promise<UserRead> {
  return get<UserRead>('/profile', undefined, signal);
}

/** `PUT /profile`. */
export function updateProfile(body: ProfileUpdate, signal?: AbortSignal): Promise<UserRead> {
  return put<UserRead>('/profile', body, signal);
}

/** `GET /profile/preferences`. */
export function getPreferences(signal?: AbortSignal): Promise<PreferencesRead> {
  return get<PreferencesRead>('/profile/preferences', undefined, signal);
}

/** `PUT /profile/preferences`. */
export function updatePreferences(
  body: PreferencesUpdate,
  signal?: AbortSignal,
): Promise<PreferencesRead> {
  return put<PreferencesRead>('/profile/preferences', body, signal);
}

// ══════════════════════════════════════════════════════════════════════════════════════
// Knowledge
// ══════════════════════════════════════════════════════════════════════════════════════

/** `GET /knowledge/sources`. */
export function listSources(
  params?: PageParams,
  signal?: AbortSignal,
): Promise<Page<SourceRead>> {
  return get<Page<SourceRead>>('/knowledge/sources', q(params), signal);
}

/** `POST /knowledge/sources` — register something for the indexer to read. */
export function createSource(body: SourceCreate, signal?: AbortSignal): Promise<SourceRead> {
  return post<SourceRead>('/knowledge/sources', body, undefined, signal);
}

/** `DELETE /knowledge/sources/{id}`. */
export function deleteSource(sourceId: Uuid, signal?: AbortSignal): Promise<OkResponse> {
  return del<OkResponse>(`/knowledge/sources/${sourceId}`, undefined, signal);
}

/** `POST /knowledge/sources/{id}/reindex`. `force` skips the fingerprint check. */
export function reindexSource(
  sourceId: Uuid,
  force = false,
  signal?: AbortSignal,
): Promise<OkResponse> {
  return post<OkResponse>(
    `/knowledge/sources/${sourceId}/reindex`,
    undefined,
    { force },
    signal,
  );
}

/** `POST /knowledge/reindex` — every enabled source for this user. */
export function reindexAll(force = false, signal?: AbortSignal): Promise<OkResponse> {
  return post<OkResponse>('/knowledge/reindex', undefined, { force }, signal);
}

/** `GET /knowledge/facts`. */
export function listFacts(
  filters?: FactFilters,
  signal?: AbortSignal,
): Promise<Page<FactRead>> {
  return get<Page<FactRead>>('/knowledge/facts', q(filters), signal);
}

/** `PATCH /knowledge/facts/{id}` — the user correcting the graph, which is itself a source. */
export function updateFact(
  factId: Uuid,
  body: FactUpdate,
  signal?: AbortSignal,
): Promise<FactRead> {
  return patch<FactRead>(`/knowledge/facts/${factId}`, body, signal);
}

/** `GET /knowledge/entities`. */
export function listEntities(
  filters?: EntityFilters,
  signal?: AbortSignal,
): Promise<Page<EntityRead>> {
  return get<Page<EntityRead>>('/knowledge/entities', q(filters), signal);
}

/** `GET /knowledge/graph` — a renderable slice, capped by `limit` (default 250). */
export function getGraph(filters?: GraphFilters, signal?: AbortSignal): Promise<GraphView> {
  return get<GraphView>('/knowledge/graph', q(filters), signal);
}

/** `GET /knowledge/search` — hybrid retrieval across facts, chunks, entities and memories. */
export function searchKnowledge(
  query: string,
  options?: { kinds?: string; limit?: number },
  signal?: AbortSignal,
): Promise<Page<KnowledgeSearchResult>> {
  return get<Page<KnowledgeSearchResult>>(
    '/knowledge/search',
    { q: query, ...q(options) },
    signal,
  );
}

/** `GET /knowledge/stats`. */
export function getKnowledgeStats(signal?: AbortSignal): Promise<KnowledgeStats> {
  return get<KnowledgeStats>('/knowledge/stats', undefined, signal);
}

// ══════════════════════════════════════════════════════════════════════════════════════
// Postings
// ══════════════════════════════════════════════════════════════════════════════════════

/** `GET /postings`. */
export function listPostings(
  filters?: PostingFilters,
  signal?: AbortSignal,
): Promise<Page<PostingRead>> {
  return get<Page<PostingRead>>('/postings', q(filters), signal);
}

/** `GET /postings/{id}`. */
export function getPosting(postingId: Uuid, signal?: AbortSignal): Promise<PostingRead> {
  return get<PostingRead>(`/postings/${postingId}`, undefined, signal);
}

/** `POST /postings/discover` — one manual discovery run, answered 202. */
export function discoverPostings(
  body: DiscoverRequest = {},
  signal?: AbortSignal,
): Promise<OkResponse> {
  return post<OkResponse>('/postings/discover', body, undefined, signal);
}

/**
 * `POST /postings/{id}/apply` — queue one posting for the apply pipeline.
 *
 * **Never optimistic** (`docs/UI.md` §10.9). It touches the outside world, so the UI flips
 * to `preparing` locally and waits for the real `application.*` event; showing "Submitted"
 * and rolling back would be a lie about something irreversible.
 */
export function applyToPosting(postingId: Uuid, signal?: AbortSignal): Promise<OkResponse> {
  return post<OkResponse>(`/postings/${postingId}/apply`, undefined, undefined, signal);
}

// ══════════════════════════════════════════════════════════════════════════════════════
// Applications
// ══════════════════════════════════════════════════════════════════════════════════════

/** `GET /applications`. */
export function listApplications(
  filters?: ApplicationFilters,
  signal?: AbortSignal,
): Promise<Page<ApplicationRead>> {
  return get<Page<ApplicationRead>>('/applications', q(filters), signal);
}

/** `GET /applications/{id}` — the detail projection, with events, artifacts and documents. */
export function getApplication(
  applicationId: Uuid,
  signal?: AbortSignal,
): Promise<ApplicationDetail> {
  return get<ApplicationDetail>(`/applications/${applicationId}`, undefined, signal);
}

/** `PATCH /applications/{id}` — status or notes. Optimistic; see `docs/UI.md` §10.9. */
export function updateApplication(
  applicationId: Uuid,
  body: ApplicationUpdate,
  signal?: AbortSignal,
): Promise<ApplicationRead> {
  return patch<ApplicationRead>(`/applications/${applicationId}`, body, signal);
}

/** `POST /applications/{id}/retry` — re-drive a failed application from its last checkpoint. */
export function retryApplication(
  applicationId: Uuid,
  signal?: AbortSignal,
): Promise<OkResponse> {
  return post<OkResponse>(`/applications/${applicationId}/retry`, undefined, undefined, signal);
}

/** `GET /applications/{id}/artifacts`. */
export function listApplicationArtifacts(
  applicationId: Uuid,
  params?: PageParams,
  signal?: AbortSignal,
): Promise<Page<ArtifactRead>> {
  return get<Page<ArtifactRead>>(
    `/applications/${applicationId}/artifacts`,
    q(params),
    signal,
  );
}

/**
 * Absolute URL of one artifact's bytes, for an `<img src>` or an `<a download>`.
 *
 * Screenshots are the reason this exists: the proof-of-submission image is rendered inline,
 * and routing it through a blob would cost a copy of every screenshot in memory.
 */
export function applicationArtifactUrl(applicationId: Uuid, fileId: Uuid): Promise<string> {
  return absoluteUrl(`${API_PREFIX}/applications/${applicationId}/artifacts/${fileId}`);
}

/** `GET /applications/{id}/artifacts/{fileId}` as a blob. */
export function downloadApplicationArtifact(
  applicationId: Uuid,
  fileId: Uuid,
  signal?: AbortSignal,
): Promise<Blob> {
  return getBlob(`/applications/${applicationId}/artifacts/${fileId}`, signal);
}

// ══════════════════════════════════════════════════════════════════════════════════════
// Review queue
// ══════════════════════════════════════════════════════════════════════════════════════

/** `GET /reviews`. Same filter signature as `/applications`. */
export function listReviews(
  filters?: ApplicationFilters,
  signal?: AbortSignal,
): Promise<Page<ReviewItem>> {
  return get<Page<ReviewItem>>('/reviews', q(filters), signal);
}

/**
 * `POST /reviews/{id}/resolve` — the human's answers to the open questions.
 *
 * **Never optimistic.** Resolving a review is what releases a real submission.
 */
export function resolveReview(
  applicationId: Uuid,
  body: ReviewResolve,
  signal?: AbortSignal,
): Promise<OkResponse> {
  return post<OkResponse>(`/reviews/${applicationId}/resolve`, body, undefined, signal);
}

/** `POST /reviews/{id}/dismiss` — abandon the application instead of answering. */
export function dismissReview(
  applicationId: Uuid,
  body: ReviewDismiss = {},
  signal?: AbortSignal,
): Promise<ApplicationRead> {
  return post<ApplicationRead>(`/reviews/${applicationId}/dismiss`, body, undefined, signal);
}

// ══════════════════════════════════════════════════════════════════════════════════════
// Resumes
// ══════════════════════════════════════════════════════════════════════════════════════

/** `GET /resumes` — variants, not versions. */
export function listResumes(
  params?: PageParams,
  signal?: AbortSignal,
): Promise<Page<ResumeRead>> {
  return get<Page<ResumeRead>>('/resumes', q(params), signal);
}

/** `POST /resumes`. */
export function createResume(body: ResumeCreate, signal?: AbortSignal): Promise<ResumeRead> {
  return post<ResumeRead>('/resumes', body, undefined, signal);
}

/** `GET /resumes/{id}` — one variant, with its version count and latest generation. */
export function getResume(resumeId: Uuid, signal?: AbortSignal): Promise<ResumeRead> {
  return get<ResumeRead>(`/resumes/${resumeId}`, undefined, signal);
}

/** `PATCH /resumes/{id}` — rename, retarget, retemplate, or make default. */
export function updateResume(
  resumeId: Uuid,
  body: ResumeUpdate,
  signal?: AbortSignal,
): Promise<ResumeRead> {
  return patch<ResumeRead>(`/resumes/${resumeId}`, body, signal);
}

/**
 * `DELETE /resumes/{id}`.
 *
 * Answers 409 when the variant has a version an employer already received: deleting it
 * would erase the record of what was submitted, so the server keeps it.
 */
export function deleteResume(resumeId: Uuid, signal?: AbortSignal): Promise<OkResponse> {
  return del<OkResponse>(`/resumes/${resumeId}`, undefined, signal);
}

/** `GET /resumes/versions/{id}` — one immutable generation with its fact provenance. */
export function getResumeVersion(
  versionId: Uuid,
  signal?: AbortSignal,
): Promise<ResumeVersionRead> {
  return get<ResumeVersionRead>(`/resumes/versions/${versionId}`, undefined, signal);
}

/** Absolute URL of a rendered resume file, for a preview iframe or a download link. */
export function resumeVersionDownloadUrl(versionId: Uuid): Promise<string> {
  return absoluteUrl(`${API_PREFIX}/resumes/versions/${versionId}/download`);
}

/** `GET /resumes/versions/{id}/download` as a blob. */
export function downloadResumeVersion(versionId: Uuid, signal?: AbortSignal): Promise<Blob> {
  return getBlob(`/resumes/versions/${versionId}/download`, signal);
}

/** `POST /resumes/preview` — tailor against a posting without applying to anything. */
export function previewResume(
  body: ResumePreviewRequest,
  signal?: AbortSignal,
): Promise<ResumeVersionRead> {
  return post<ResumeVersionRead>('/resumes/preview', body, undefined, signal);
}

// ══════════════════════════════════════════════════════════════════════════════════════
// Sessions
// ══════════════════════════════════════════════════════════════════════════════════════

/** `GET /sessions`. */
export function listSessions(
  params?: PageParams,
  signal?: AbortSignal,
): Promise<Page<SessionRead>> {
  return get<Page<SessionRead>>('/sessions', q(params), signal);
}

/** `GET /sessions/{id}` — the run plus the applications it produced. */
export function getSession(sessionId: Uuid, signal?: AbortSignal): Promise<SessionDetail> {
  return get<SessionDetail>(`/sessions/${sessionId}`, undefined, signal);
}

/** `POST /sessions/start`. **Never optimistic** — it starts real work. */
export function startSession(
  body: SessionStartRequest = {},
  signal?: AbortSignal,
): Promise<OkResponse> {
  return post<OkResponse>('/sessions/start', body, undefined, signal);
}

/** `POST /sessions/{id}/stop`. Destructive: `Ctrl+S` must confirm first (`docs/UI.md` §9.2). */
export function stopSession(sessionId: Uuid, signal?: AbortSignal): Promise<SessionRead> {
  return post<SessionRead>(`/sessions/${sessionId}/stop`, undefined, undefined, signal);
}

// ══════════════════════════════════════════════════════════════════════════════════════
// Analytics
// ══════════════════════════════════════════════════════════════════════════════════════

/** `GET /analytics/overview` — stats, funnel, timeseries and insights in one request. */
export function getAnalyticsOverview(
  days = 30,
  signal?: AbortSignal,
): Promise<AnalyticsOverview> {
  return get<AnalyticsOverview>('/analytics/overview', { days }, signal);
}

/** `GET /analytics/funnel`. */
export function getFunnel(signal?: AbortSignal): Promise<Page<FunnelStage>> {
  return get<Page<FunnelStage>>('/analytics/funnel', undefined, signal);
}

/** `GET /analytics/timeseries`. */
export function getTimeseries(
  days = 30,
  signal?: AbortSignal,
): Promise<Page<TimeseriesPoint>> {
  return get<Page<TimeseriesPoint>>('/analytics/timeseries', { days }, signal);
}

/** `GET /analytics/insights`. */
export function getInsights(signal?: AbortSignal): Promise<Page<InsightItem>> {
  return get<Page<InsightItem>>('/analytics/insights', undefined, signal);
}

// ══════════════════════════════════════════════════════════════════════════════════════
// Settings
// ══════════════════════════════════════════════════════════════════════════════════════

/** `GET /settings` — the redacted projection; secrets appear only as `*_configured`. */
export function getSettings(signal?: AbortSignal): Promise<SettingsRead> {
  return get<SettingsRead>('/settings', undefined, signal);
}

/** `PUT /settings`. The kill switch lives here: `auto_apply_enabled` and `dry_run`. */
export function updateSettings(
  body: SettingsUpdate,
  signal?: AbortSignal,
): Promise<SettingsRead> {
  return put<SettingsRead>('/settings', body, signal);
}

/** `GET /settings/plugins`. `probe` costs one live call per plugin, so it is opt-in. */
export function listPlugins(
  filters?: PluginFilters,
  signal?: AbortSignal,
): Promise<Page<PluginInfo>> {
  return get<Page<PluginInfo>>('/settings/plugins', q(filters), signal);
}

/** `GET /settings/scoring-rules` — the editable form of `scoring_rules.yaml`. */
export function getScoringRules(signal?: AbortSignal): Promise<Page<ScoreRuleSchema>> {
  return get<Page<ScoreRuleSchema>>('/settings/scoring-rules', undefined, signal);
}

/** `PUT /settings/scoring-rules` — replaces the whole pack, or resets it to defaults. */
export function updateScoringRules(
  body: ScoringRulesUpdate,
  signal?: AbortSignal,
): Promise<Page<ScoreRuleSchema>> {
  return put<Page<ScoreRuleSchema>>('/settings/scoring-rules', body, signal);
}

// ══════════════════════════════════════════════════════════════════════════════════════
// Logs
// ══════════════════════════════════════════════════════════════════════════════════════

/**
 * `GET /logs` — historical search only.
 *
 * The live tail is **not** a query (`docs/UI.md` §7.21): it is a module-level ring buffer in
 * `lib/log-buffer.ts`. An append-per-line query entry would churn the cache and trigger the
 * persister on every line.
 */
export function listLogs(
  filters?: LogFilters,
  signal?: AbortSignal,
): Promise<Page<LogEntryRead>> {
  return get<Page<LogEntryRead>>('/logs', q(filters), signal);
}

// ══════════════════════════════════════════════════════════════════════════════════════
// Status sync — docs/CONTRACTS.md §17.7
// ══════════════════════════════════════════════════════════════════════════════════════

/** `GET /tracking/accounts`. A bare array, not a `Page`. */
export function listEmailAccounts(signal?: AbortSignal): Promise<EmailAccountRead[]> {
  return get<EmailAccountRead[]>('/tracking/accounts', undefined, signal);
}

/** `POST /tracking/accounts` — connect one mailbox. The secret goes to the OS keychain. */
export function createEmailAccount(
  body: EmailAccountCreate,
  signal?: AbortSignal,
): Promise<EmailAccountRead> {
  return post<EmailAccountRead>('/tracking/accounts', body, undefined, signal);
}

/** `DELETE /tracking/accounts/{id}` — also deletes the credential reference and the cursor. */
export function deleteEmailAccount(
  accountId: Uuid,
  signal?: AbortSignal,
): Promise<OkResponse> {
  return del<OkResponse>(`/tracking/accounts/${accountId}`, undefined, signal);
}

/** `POST /tracking/accounts/{id}/test` — a read-only connection probe. */
export function testEmailAccount(accountId: Uuid, signal?: AbortSignal): Promise<OkResponse> {
  return post<OkResponse>(`/tracking/accounts/${accountId}/test`, undefined, undefined, signal);
}

/** `POST /tracking/sync` — run a sync now, for one account or all of them. */
export function runSync(body: SyncRequest = {}, signal?: AbortSignal): Promise<OkResponse> {
  return post<OkResponse>('/tracking/sync', body, undefined, signal);
}

/** `POST /tracking/detect-ghosted` — apply the silence rule, gated by `ghosted_after_days`. */
export function detectGhosted(signal?: AbortSignal): Promise<OkResponse> {
  return post<OkResponse>('/tracking/detect-ghosted', undefined, undefined, signal);
}

/** `GET /tracking/signals`. */
export function listSignals(
  filters?: SignalFilters,
  signal?: AbortSignal,
): Promise<Page<StatusSignalRead>> {
  return get<Page<StatusSignalRead>>('/tracking/signals', q(filters), signal);
}

/** `POST /tracking/signals/{id}/resolve` — bind a signal to an application and apply it. */
export function resolveSignal(
  signalId: Uuid,
  body: SignalResolve,
  signal?: AbortSignal,
): Promise<ApplicationRead> {
  return post<ApplicationRead>(
    `/tracking/signals/${signalId}/resolve`,
    body,
    undefined,
    signal,
  );
}

/** `POST /tracking/signals/{id}/dismiss`. */
export function dismissSignal(signalId: Uuid, signal?: AbortSignal): Promise<OkResponse> {
  return post<OkResponse>(`/tracking/signals/${signalId}/dismiss`, undefined, undefined, signal);
}

/** `GET /tracking/stats`. */
export function getTrackingStats(signal?: AbortSignal): Promise<TrackingStats> {
  return get<TrackingStats>('/tracking/stats', undefined, signal);
}
