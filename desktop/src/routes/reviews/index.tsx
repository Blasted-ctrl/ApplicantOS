/**
 * The review queue (`docs/UI.md` §8.6) — the highest-stakes screen in the product.
 *
 * A card list rather than a table, because every item needs a decision and a decision needs
 * context. The first card is expanded and the rest are collapsed; `j`/`k` moves and expands as
 * it goes, with **no accordion animation on keyboard movement** — at forty items a night, an
 * animated expand is 300ms of latency forty times over (§P5).
 *
 * **The empty state here is a good state.** "Nothing needs you" with no call to action: the
 * agent handled everything on its own, and prompting the user to do something would turn a
 * success into a chore.
 *
 * `Resolve all safe` exists but is honest about its own preconditions. It can only act on an
 * item where *every* open field carries a suggestion the agent itself scored above your
 * confidence floor — which is rare, because a field the agent could answer confidently would
 * not have reached this queue. When there is nothing safe to resolve, the button says so
 * rather than pretending.
 */

import { ShieldCheck } from 'lucide-react';
import { useEffect, useMemo, useState } from 'react';

import { Page, ResultCount, ToolbarSpacer } from '@/components/page';
import { ReviewCard } from '@/components/review-card';
import {
  Button,
  ConfirmDialog,
  EmptyState,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  Tooltip,
} from '@/components/ui';
import {
  useDismissReview,
  useResolveReview,
  useReviews,
  useSafetyState,
  useSettings,
} from '@/hooks';
import { useShortcuts } from '@/hooks/use-shortcuts';
import { REVIEW_REASONS, type JsonObject, type ReviewItem, type ReviewReason } from '@/lib/api/types';
import { formatRelative, reviewReasonLabel } from '@/lib/utils';
import { useFiltersStore } from '@/stores/filters';

/** Fallback floor when settings have not arrived. Matches the backend default. */
const DEFAULT_MIN_CONFIDENCE = 0.75;

/** Whether every open field on an item carries a suggestion above the floor. */
function isSafeToResolve(item: ReviewItem, floor: number): boolean {
  const fields = item.unanswered_fields;
  if (fields.length === 0) return false;
  return fields.every(
    (field) =>
      field.kind !== 'file' &&
      field.suggested_answer != null &&
      field.suggested_answer !== '' &&
      field.confidence != null &&
      field.confidence >= floor,
  );
}

/** Build the answer payload from an item's own suggestions. */
function safeAnswers(item: ReviewItem): JsonObject {
  const answers: JsonObject = {};
  for (const field of item.unanswered_fields) {
    if (field.suggested_answer != null) answers[field.selector] = field.suggested_answer;
  }
  return answers;
}

/** The queue. */
export function ReviewsRoute() {
  const filters = useFiltersStore((state) => state.reviews);
  const setFilters = useFiltersStore((state) => state.setReviews);
  const reviews = useReviews(filters);
  const settings = useSettings();
  const safety = useSafetyState();
  const resolve = useResolveReview();
  const dismiss = useDismissReview();

  const items = useMemo(() => reviews.data?.items ?? [], [reviews.data]);
  const total = reviews.data?.total ?? 0;
  const floor = settings.data?.min_answer_confidence ?? DEFAULT_MIN_CONFIDENCE;

  const [focusedIndex, setFocusedIndex] = useState(0);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [confirmBulk, setConfirmBulk] = useState(false);

  // The first item is expanded by default and stays authoritative as the queue changes under
  // the user — an item resolving must not silently collapse the one that takes its place.
  useEffect(() => {
    const first = items[0];
    setExpandedId((current) => {
      if (current !== null && items.some((item) => item.application.id === current)) return current;
      return first?.application.id ?? null;
    });
  }, [items]);

  const focused = items[focusedIndex];
  const safeItems = useMemo(
    () => items.filter((item) => isSafeToResolve(item, floor)),
    [items, floor],
  );

  useShortcuts({
    'list.next': () => {
      const next = Math.min(focusedIndex + 1, Math.max(items.length - 1, 0));
      setFocusedIndex(next);
      setExpandedId(items[next]?.application.id ?? null);
    },
    'list.prev': () => {
      const next = Math.max(focusedIndex - 1, 0);
      setFocusedIndex(next);
      setExpandedId(items[next]?.application.id ?? null);
    },
    'review.expand': () => {
      setExpandedId((current) =>
        current === focused?.application.id ? null : (focused?.application.id ?? null),
      );
    },
  });

  const oldest = items[items.length - 1]?.application.updated_at;

  return (
    <Page
      title="Review queue"
      subtitle={
        total === 0
          ? 'Nothing is blocked on a human right now.'
          : `${String(total)} ${total === 1 ? 'item' : 'items'}${oldest === undefined ? '' : ` · oldest ${formatRelative(oldest)}`} · blocking ${String(total)} ${total === 1 ? 'application' : 'applications'}`
      }
      busy={reviews.isFetching}
      actions={
        <Tooltip
          content={
            safeItems.length === 0
              ? 'Nothing here can be resolved without you. Every open field either has no suggestion or scored below your confidence floor — which is exactly why it stopped.'
              : `Applies the agent's own suggestions to ${String(safeItems.length)} ${safeItems.length === 1 ? 'item' : 'items'} whose every field cleared your confidence floor.`
          }
        >
          <span>
            <Button
              variant="secondary"
              disabled={safeItems.length === 0}
              leadingIcon={<ShieldCheck aria-hidden="true" />}
              onClick={() => {
                setConfirmBulk(true);
              }}
            >
              Resolve all safe
            </Button>
          </span>
        </Tooltip>
      }
      toolbar={
        <>
          <Select
            value={filters.review_reason ?? 'all'}
            onValueChange={(value) => {
              setFilters({
                review_reason: value === 'all' ? undefined : (value as ReviewReason),
              });
            }}
          >
            <SelectTrigger size="sm" className="w-56" aria-label="Filter by reason">
              <SelectValue placeholder="Reason" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">Any reason</SelectItem>
              {REVIEW_REASONS.map((reason) => (
                <SelectItem key={reason} value={reason}>
                  {reviewReasonLabel(reason)}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <ToolbarSpacer />
          <ResultCount count={items.length} total={total} noun="waiting" />
        </>
      }
    >
      {items.length === 0 ? (
        <EmptyState
          icon={ShieldCheck}
          title="Nothing needs you"
          description={
            settings.data === undefined
              ? 'The agent handled everything on its own.'
              : safety.submissionAllowed
                ? 'The agent handled everything on its own. Anything it could not answer would appear here instead of being guessed at.'
                : 'The agent handled everything on its own. Submission is currently disabled, so nothing has been sent — arm both switches in Settings when you are ready.'
          }
        />
      ) : (
        <div className="mx-auto flex max-w-[900px] flex-col gap-3">
          {items.map((item, index) => (
            <ReviewCard
              key={item.application.id}
              item={item}
              expanded={expandedId === item.application.id}
              focused={focusedIndex === index}
              minConfidence={floor}
              submissionAllowed={safety.submissionAllowed}
              safetyWarning={safety.warning}
              resolving={resolve.isPending}
              onToggle={() => {
                setFocusedIndex(index);
                setExpandedId((current) =>
                  current === item.application.id ? null : item.application.id,
                );
              }}
              onResolve={(answers) => {
                resolve.mutate({ id: item.application.id, body: { answers } });
              }}
              onDismiss={(note) => {
                dismiss.mutate(
                  note === '' ? { id: item.application.id } : { id: item.application.id, note },
                );
              }}
            />
          ))}
        </div>
      )}

      <ConfirmDialog
        open={confirmBulk}
        onOpenChange={setConfirmBulk}
        destructive={false}
        title={`Resolve ${String(safeItems.length)} ${safeItems.length === 1 ? 'item' : 'items'}?`}
        description={
          safety.submissionAllowed ? (
            <>
              Each one is sent back to the apply queue with the agent&apos;s own answers, and
              ApplicantOS will submit them. Submissions cannot be taken back.
            </>
          ) : (
            <>
              Each one is sent back to the apply queue with the agent&apos;s own answers. Because{' '}
              {safety.warning ?? 'submission is disabled'}, nothing will be sent to any employer.
            </>
          )
        }
        confirmLabel={`Resolve ${String(safeItems.length)}`}
        loading={resolve.isPending}
        onConfirm={() => {
          setConfirmBulk(false);
          for (const item of safeItems) {
            resolve.mutate({
              id: item.application.id,
              body: { answers: safeAnswers(item) },
            });
          }
        }}
      />
    </Page>
  );
}
