/**
 * Résumés: the named variants, one immutable version, and the tailoring preview.
 *
 * Golden rule #6 shapes this family: a résumé is a **generated view** over the knowledge
 * graph, so `ResumeRead` is configuration and `ResumeVersionRead.content_json` is the
 * permanent artefact. The rendered PDF is disposable and is fetched as a blob only when
 * something is about to display or save it — never held in the query cache, where it would be
 * a multi-megabyte entry the persister would faithfully write to disk on every change.
 *
 * `POST /resumes/preview` is a mutation rather than a query even though it reads nothing: it
 * *generates* a version, costs tokens, and must not be re-run because a component remounted.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import * as api from '@/lib/api/endpoints';
import type {
  PageParams,
  ResumeCreate,
  ResumePreviewRequest,
  ResumeUpdate,
  Uuid,
} from '@/lib/api/types';
import { notifyError } from '@/lib/notify';
import { qk } from '@/lib/query/keys';
import { resumeListOptions, resumeVersionOptions } from '@/lib/query/options';

/** `GET /resumes` — variants, not versions. */
export function useResumes(params: PageParams = {}) {
  return useQuery(resumeListOptions(params));
}

/** `GET /resumes/versions/{id}` — one generation, with the fact ids behind every bullet. */
export function useResumeVersion(id: Uuid | undefined) {
  return useQuery({
    ...resumeVersionOptions(id ?? ''),
    enabled: id !== undefined && id !== '',
  });
}

/** `POST /resumes`. */
export function useCreateResume() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (body: ResumeCreate) => api.createResume(body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: qk.resumes(), refetchType: 'active' });
    },
    onError: (error) => {
      notifyError('Could not create the résumé', error);
    },
  });
}

/** `GET /resumes/{id}` — one variant. */
export function useResume(id: Uuid | undefined) {
  return useQuery({
    queryKey: qk.resumeDetail(id ?? ''),
    queryFn: ({ signal }) => api.getResume(id ?? '', signal),
    enabled: id !== undefined && id !== '',
  });
}

/**
 * `PATCH /resumes/{id}` — **not optimistic**.
 *
 * Making a variant default clears the flag on every other one, which is a change to rows
 * this mutation does not name. Writing that locally would mean predicting a multi-row edit
 * and rolling back a guess; invalidating is both simpler and honest about what changed.
 */
export function useUpdateResume() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({ id, body }: { id: Uuid; body: ResumeUpdate }) => api.updateResume(id, body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: qk.resumes(), refetchType: 'active' });
    },
    onError: (error) => {
      notifyError('Could not update the résumé', error);
    },
  });
}

/**
 * `DELETE /resumes/{id}` — **not optimistic**, and the reason is the 409.
 *
 * The server refuses when the variant has a version an employer already received. Removing
 * the row on click and putting it back on refusal would show the user a deletion that did
 * not happen, for the one case where the refusal is the important part of the answer.
 */
export function useDeleteResume() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (id: Uuid) => api.deleteResume(id),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: qk.resumes(), refetchType: 'active' });
    },
    onError: (error) => {
      notifyError('Could not delete the résumé', error);
    },
  });
}

/**
 * `POST /resumes/preview` — tailor against a posting without applying to anything.
 *
 * The generated version is written into the cache on success so the preview pane reads it
 * from the same key `GET /resumes/versions/{id}` would populate. That is what lets the user
 * navigate away and come back without regenerating — and regenerating is not free.
 */
export function usePreviewResume() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (body: ResumePreviewRequest) => api.previewResume(body),
    onSuccess: (version) => {
      queryClient.setQueryData(qk.resumeVersion(version.id), version);
      void queryClient.invalidateQueries({ queryKey: qk.resumes(), refetchType: 'active' });
    },
    onError: (error) => {
      notifyError('Could not generate the résumé', error);
    },
  });
}
