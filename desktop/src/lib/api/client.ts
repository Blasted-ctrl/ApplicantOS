/**
 * The typed fetch client. Every HTTP call in the renderer goes through `request()`.
 *
 * Four things this file is responsible for, none of which belong in a route or a hook:
 *
 * **Origin resolution.** The base URL comes from the Tauri shell's `backend_port()` command,
 * resolved lazily and memoised in `lib/tauri.ts`. Until the sidecar reports ready that call
 * is pending, so the first request of a cold start legitimately waits — and that wait is the
 * only place in the app where waiting is correct, because there is nothing to render yet.
 *
 * **A correlation id on every request.** The backend's middleware honours a client-supplied
 * `X-Request-ID` and binds it to every log line the request produces. Generating it here
 * rather than reading it off the response means a request that *never got a response* — a
 * dropped socket, a killed sidecar — still names the log lines it produced. That id rides on
 * the thrown {@link ApiError}, and `docs/UI.md` §7.15 puts it in the error toast.
 *
 * **One error type.** Every failure — HTTP status, network, abort, malformed body — arrives
 * as an {@link ApiError} with a stable `code`. Callers branch on `code`, never on `detail`,
 * because `detail` is prose written for a human and is not part of the contract.
 *
 * **Query serialisation that matches the cache key.** `undefined`, `null` and `''` are
 * dropped from the query string, exactly as `normalizeFilters` drops them from the query
 * key. If the two disagreed, two keys that hash differently would issue identical requests —
 * which is the cache-fragmentation failure `docs/UI.md` §10.4 describes, arriving from the
 * other direction.
 */

import { resolveBaseUrl } from '@/lib/tauri';

import { API_PREFIX, type ApiErrorCode, type ErrorResponse } from './types';

/** Header the backend's middleware reads for a client-supplied correlation id. */
const REQUEST_ID_HEADER = 'X-Request-ID';

/** Header the backend echoes the resolved correlation id back on. */
const CORRELATION_ID_HEADER = 'X-Correlation-ID';

/** Header carrying the acting user, for the multi-user shape the API was built against. */
const USER_ID_HEADER = 'X-User-Id';

/**
 * Ceiling on a single request.
 *
 * The backend is on loopback, so a request that has not answered in thirty seconds is not
 * slow — it is a sidecar that died with the socket still open, and the honest response is to
 * fail with something the UI can render rather than to hang a mutation forever.
 */
const DEFAULT_TIMEOUT_MS = 30_000;

/** Value a query parameter may take before serialisation. */
export type QueryValue = string | number | boolean | null | undefined | readonly string[];

/** Query parameters accepted by {@link request}. */
export type QueryParams = Record<string, QueryValue>;

/** Options for one request. */
export interface RequestOptions {
  method?: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE';
  query?: QueryParams;
  /** Serialised as JSON. `undefined` sends no body; `null` sends a literal `null`. */
  body?: unknown;
  signal?: AbortSignal;
  /** Overrides {@link DEFAULT_TIMEOUT_MS}. Pass `0` to disable the timeout entirely. */
  timeoutMs?: number;
  headers?: Record<string, string>;
}

/**
 * Every failure this client produces.
 *
 * `code` is the branchable value: it is the backend's stable machine code when the server
 * answered, and one of the four client-side codes below when it did not.
 */
export class ApiError extends Error {
  /** HTTP status, or `0` when no response was received. */
  readonly status: number;

  /** Stable machine code. See `app/api/errors.py` for the server-side set. */
  readonly code: ApiErrorCode;

  /** Human-readable explanation, safe to display. Never branch on this. */
  readonly detail: string | null;

  /** The id that finds this request's log lines. Always present. */
  readonly correlationId: string;

  /** Path that failed, without the origin — the origin is not useful in a toast. */
  readonly path: string;

  constructor(init: {
    message: string;
    status: number;
    code: ApiErrorCode;
    detail?: string | null;
    correlationId: string;
    path: string;
    cause?: unknown;
  }) {
    super(init.message, init.cause === undefined ? undefined : { cause: init.cause });
    this.name = 'ApiError';
    this.status = init.status;
    this.code = init.code;
    this.detail = init.detail ?? null;
    this.correlationId = init.correlationId;
    this.path = init.path;
  }

  /** Whether retrying this request could plausibly succeed. Drives the toast's Retry action. */
  get isRetryable(): boolean {
    if (this.status === 0) return this.code !== 'request_aborted';
    if (this.status === 429) return true;
    return this.status >= 500;
  }

  /** Whether the failure is the backend not being up yet, rather than a rejected request. */
  get isBackendUnavailable(): boolean {
    return this.code === 'backend_unavailable' || this.code === 'network_error';
  }
}

/** Client-side codes, distinct from anything the server can return. */
const CODE_NETWORK: ApiErrorCode = 'network_error';
const CODE_ABORTED: ApiErrorCode = 'request_aborted';
const CODE_TIMEOUT: ApiErrorCode = 'request_timeout';
const CODE_BACKEND_DOWN: ApiErrorCode = 'backend_unavailable';
const CODE_BAD_RESPONSE: ApiErrorCode = 'malformed_response';

/** The acting user, when the app has been told about one. `null` lets the backend default. */
let actingUserId: string | null = null;

/** Set the `X-User-Id` header sent with every subsequent request. */
export function setActingUser(userId: string | null): void {
  actingUserId = userId;
}

/** The acting user id currently being sent, if any. */
export function getActingUser(): string | null {
  return actingUserId;
}

/** A fresh correlation id. `crypto.randomUUID` is available in all three target webviews. */
function newCorrelationId(): string {
  return crypto.randomUUID();
}

/**
 * Serialise query parameters, dropping anything that would fragment the cache.
 *
 * Array values repeat the key, which is how FastAPI reads a `list[str]` query parameter.
 */
export function buildQuery(params: QueryParams | undefined): string {
  if (params === undefined) return '';
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === '') continue;
    if (Array.isArray(value)) {
      for (const item of value) {
        if (item !== '') search.append(key, item);
      }
      continue;
    }
    search.append(key, String(value));
  }
  const query = search.toString();
  return query === '' ? '' : `?${query}`;
}

/**
 * Combine the caller's signal with a timeout.
 *
 * Written by hand rather than with `AbortSignal.any`, which WKWebView only gained in Safari
 * 17.4 — one minor version above the baseline in `vite.config.ts`.
 */
function withTimeout(
  signal: AbortSignal | undefined,
  timeoutMs: number,
): { signal: AbortSignal; dispose: () => void; timedOut: () => boolean } {
  const controller = new AbortController();
  let didTimeOut = false;

  const onAbort = (): void => {
    controller.abort(signal?.reason);
  };

  if (signal !== undefined) {
    if (signal.aborted) controller.abort(signal.reason);
    else signal.addEventListener('abort', onAbort, { once: true });
  }

  const timer =
    timeoutMs > 0
      ? window.setTimeout(() => {
          didTimeOut = true;
          controller.abort();
        }, timeoutMs)
      : undefined;

  return {
    signal: controller.signal,
    dispose: () => {
      if (timer !== undefined) window.clearTimeout(timer);
      signal?.removeEventListener('abort', onAbort);
    },
    timedOut: () => didTimeOut,
  };
}

/** Read the error body, tolerating a non-JSON response from a proxy or a crash page. */
async function readErrorBody(response: Response): Promise<ErrorResponse | null> {
  try {
    const parsed: unknown = await response.json();
    if (typeof parsed === 'object' && parsed !== null && 'error' in parsed) {
      return parsed as ErrorResponse;
    }
    return null;
  } catch {
    return null;
  }
}

/**
 * Perform one request against the backend.
 *
 * @typeParam T - The response body's shape. `void` for endpoints that answer 204.
 * @param path - Path relative to the origin, including the `/api/v1` prefix where it applies.
 */
export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = 'GET', query, body, signal, timeoutMs = DEFAULT_TIMEOUT_MS, headers } = options;
  const correlationId = newCorrelationId();
  const url = `${path}${buildQuery(query)}`;

  let origin: string;
  try {
    origin = await resolveBaseUrl();
  } catch (cause) {
    throw new ApiError({
      message: 'The ApplicantOS backend is not running yet.',
      status: 0,
      code: CODE_BACKEND_DOWN,
      detail: cause instanceof Error ? cause.message : String(cause),
      correlationId,
      path: url,
      cause,
    });
  }

  const requestHeaders: Record<string, string> = {
    Accept: 'application/json',
    [REQUEST_ID_HEADER]: correlationId,
    ...(actingUserId === null ? {} : { [USER_ID_HEADER]: actingUserId }),
    ...headers,
  };
  if (body !== undefined) requestHeaders['Content-Type'] = 'application/json';

  const timeout = withTimeout(signal, timeoutMs);
  let response: Response;
  try {
    response = await fetch(`${origin}${url}`, {
      method,
      headers: requestHeaders,
      signal: timeout.signal,
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
  } catch (cause) {
    const aborted = timeout.signal.aborted;
    const isTimeout = timeout.timedOut();
    timeout.dispose();
    throw new ApiError({
      message: isTimeout
        ? 'The backend did not respond in time.'
        : aborted
          ? 'The request was cancelled.'
          : 'Could not reach the ApplicantOS backend.',
      status: 0,
      code: isTimeout ? CODE_TIMEOUT : aborted ? CODE_ABORTED : CODE_NETWORK,
      detail: cause instanceof Error ? cause.message : String(cause),
      correlationId,
      path: url,
      cause,
    });
  }
  timeout.dispose();

  // Prefer the id the server actually bound; it is the one in the log lines. They agree
  // unless a proxy rewrote the header, in which case the server's is the useful one.
  const serverCorrelationId = response.headers.get(CORRELATION_ID_HEADER) ?? correlationId;

  if (!response.ok) {
    const errorBody = await readErrorBody(response);
    throw new ApiError({
      message: errorBody?.detail ?? `Request failed with status ${response.status}.`,
      status: response.status,
      code: errorBody?.error ?? `http_${response.status}`,
      detail: errorBody?.detail ?? null,
      correlationId: errorBody?.correlation_id ?? serverCorrelationId,
      path: url,
    });
  }

  if (response.status === 204 || response.headers.get('Content-Length') === '0') {
    return undefined as T;
  }

  try {
    return (await response.json()) as T;
  } catch (cause) {
    throw new ApiError({
      message: 'The backend returned a response this client could not read.',
      status: response.status,
      code: CODE_BAD_RESPONSE,
      detail: cause instanceof Error ? cause.message : String(cause),
      correlationId: serverCorrelationId,
      path: url,
      cause,
    });
  }
}

/** `GET` a path under `/api/v1`. */
export function get<T>(path: string, query?: QueryParams, signal?: AbortSignal): Promise<T> {
  return request<T>(`${API_PREFIX}${path}`, { method: 'GET', query, signal });
}

/** `POST` a JSON body to a path under `/api/v1`. */
export function post<T>(
  path: string,
  body?: unknown,
  query?: QueryParams,
  signal?: AbortSignal,
): Promise<T> {
  return request<T>(`${API_PREFIX}${path}`, { method: 'POST', body, query, signal });
}

/** `PUT` a JSON body to a path under `/api/v1`. */
export function put<T>(path: string, body?: unknown, signal?: AbortSignal): Promise<T> {
  return request<T>(`${API_PREFIX}${path}`, { method: 'PUT', body, signal });
}

/** `PATCH` a JSON body to a path under `/api/v1`. */
export function patch<T>(path: string, body?: unknown, signal?: AbortSignal): Promise<T> {
  return request<T>(`${API_PREFIX}${path}`, { method: 'PATCH', body, signal });
}

/** `DELETE` a path under `/api/v1`. */
export function del<T>(path: string, query?: QueryParams, signal?: AbortSignal): Promise<T> {
  return request<T>(`${API_PREFIX}${path}`, { method: 'DELETE', query, signal });
}

/**
 * Absolute URL for a path on the backend.
 *
 * Needed for the handful of things the browser fetches itself rather than through this
 * client: an `<img src>` pointing at a submission screenshot, or an `<a download>` for a
 * rendered resume. Everything else goes through {@link request}.
 */
export async function absoluteUrl(path: string): Promise<string> {
  const origin = await resolveBaseUrl();
  return `${origin}${path.startsWith('/') ? path : `/${path}`}`;
}

/**
 * Download a binary artifact as a blob.
 *
 * The two `application/octet-stream` endpoints — artifact bytes and a rendered resume — are
 * the only responses in the API that are not JSON, so they get their own path rather than a
 * response-type switch inside {@link request}.
 */
export async function getBlob(path: string, signal?: AbortSignal): Promise<Blob> {
  const correlationId = newCorrelationId();
  const origin = await resolveBaseUrl();
  const response = await fetch(`${origin}${API_PREFIX}${path}`, {
    headers: {
      [REQUEST_ID_HEADER]: correlationId,
      ...(actingUserId === null ? {} : { [USER_ID_HEADER]: actingUserId }),
    },
    ...(signal === undefined ? {} : { signal }),
  });
  if (!response.ok) {
    const errorBody = await readErrorBody(response);
    throw new ApiError({
      message: errorBody?.detail ?? `Download failed with status ${response.status}.`,
      status: response.status,
      code: errorBody?.error ?? `http_${response.status}`,
      detail: errorBody?.detail ?? null,
      correlationId: errorBody?.correlation_id ?? correlationId,
      path,
    });
  }
  return response.blob();
}
