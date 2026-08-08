/**
 * One item in the review queue — the highest-stakes surface in the product
 * (`docs/UI.md` §8.6).
 *
 * Every entry here exists because the agent refused to guess (golden rule #2), so this card's
 * job is to make a human's decision *informed* rather than merely quick. Four rules follow
 * from that, and none of them is negotiable:
 *
 * **The consequence pair comes before the buttons.** What happens if you approve, and what
 * happens if you dismiss, in plain language, above both actions. The approve copy changes with
 * the kill switch, because "this will be sent to Datadog" and "nothing will be sent because
 * dry run is on" are different facts and the user is entitled to know which one they are
 * about to cause.
 *
 * **Confidence is a number, never a word.** `0.62` is shown as `0.62`, and below
 * `min_answer_confidence` it takes a `--st-review` chip and a review rail. Softening it into
 * "fairly confident" would be exactly the fabrication the review queue exists to prevent.
 *
 * **Nothing is invented to fill the layout.** The pipeline records a field's selector, label,
 * kind, options, max length and hint; it does not record provenance chips for a suggestion it
 * never made. Where the payload carries a suggested answer and its confidence, both are shown;
 * where it does not, the field is simply empty and says so.
 *
 * **Dismiss is destructive and confirms.** `Ctrl+X` in the irreversible namespace (§9.1), with
 * a dialog naming the company, because `abandoned` is terminal.
 */

import { AlertTriangle } from 'lucide-react';
import { useCallback, useMemo, useState, type ReactNode } from 'react';

import {
  Badge,
  Button,
  Card,
  CardBody,
  CardHeader,
  CharacterCounter,
  CheckboxField,
  ConfirmDialog,
  Input,
  Kbd,
  Label,
  Radio,
  RadioGroup,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  StatusDot,
  Textarea,
  Tooltip,
} from '@/components/ui';
import type { JsonObject, ReviewField, ReviewItem } from '@/lib/api/types';
import {
  cn,
  formatRelative,
  orDash,
  providerLabel,
  reviewReasonLabel,
  shortId,
  statusTone,
} from '@/lib/utils';

/**
 * Draft answers, kept per application for the life of the process.
 *
 * There is no draft endpoint and inventing one client-side would be a promise the backend
 * cannot keep, so `Save draft` is exactly what it says on the tooltip: your typing survives
 * navigating away and coming back, on this device, until the app closes. Nothing is sent.
 */
const DRAFTS = new Map<string, Record<string, string>>();

/** Props for {@link ReviewCard}. */
export interface ReviewCardProps {
  item: ReviewItem;
  expanded: boolean;
  onToggle: () => void;
  /** Roving focus from `j`/`k`, so the keyboard user can see where they are. */
  focused?: boolean;
  /** `settings.min_answer_confidence` — the floor below which a suggestion is flagged. */
  minConfidence: number;
  /** `settings.is_submission_allowed`. Decides which approve consequence is true. */
  submissionAllowed: boolean;
  /** `Dry run` / `Auto-apply off`, or `null` when both switches are armed. */
  safetyWarning: string | null;
  onResolve: (answers: JsonObject) => void;
  onDismiss: (note: string) => void;
  resolving?: boolean;
}

/** One decision, with everything needed to make it. */
export function ReviewCard({
  item,
  expanded,
  onToggle,
  focused = false,
  minConfidence,
  submissionAllowed,
  safetyWarning,
  onResolve,
  onDismiss,
  resolving = false,
}: ReviewCardProps) {
  const application = item.application;
  const company = application.company?.name ?? application.posting?.company?.name ?? 'this employer';
  const role = application.posting?.title;
  const provider = application.posting?.provider;
  const tone = statusTone(application.status);

  const [answers, setAnswers] = useState<Record<string, string>>(
    () => DRAFTS.get(application.id) ?? {},
  );
  const [confirmDismiss, setConfirmDismiss] = useState(false);
  const [note, setNote] = useState('');

  const setAnswer = useCallback((selector: string, value: string) => {
    setAnswers((current) => ({ ...current, [selector]: value }));
  }, []);

  const answerable = item.unanswered_fields.filter((field) => field.kind !== 'file');
  const uploads = item.unanswered_fields.filter((field) => field.kind === 'file');

  const missingRequired = useMemo(
    () =>
      answerable.filter(
        (field) => field.required && (answers[field.selector] ?? '').trim() === '',
      ),
    [answerable, answers],
  );

  const filledCount = answerable.filter(
    (field) => (answers[field.selector] ?? '').trim() !== '',
  ).length;

  const saveDraft = useCallback(() => {
    DRAFTS.set(application.id, answers);
  }, [application.id, answers]);

  const submit = useCallback(() => {
    const payload: JsonObject = {};
    for (const [selector, value] of Object.entries(answers)) {
      if (value.trim() !== '') payload[selector] = value;
    }
    if (Object.keys(payload).length === 0) return;
    DRAFTS.delete(application.id);
    onResolve(payload);
  }, [answers, application.id, onResolve]);

  return (
    <Card
      selected={focused}
      className={cn('scroll-mt-4', focused && 'shadow-selected')}
      data-review-id={application.id}
    >
      <CardHeader
        title={
          <button
            type="button"
            onClick={onToggle}
            aria-expanded={expanded}
            className="flex min-w-0 items-center gap-2 text-left"
          >
            <StatusDot status={application.status} aria-hidden="true" />
            <span className="truncate font-display text-md font-semibold text-primary">
              {orDash(company)}
              {role === null || role === undefined ? '' : ` · ${role}`}
            </span>
          </button>
        }
        subtitle={
          <span className="flex flex-wrap items-center gap-2">
            {provider !== undefined && (
              <span className="chip-mono">{providerLabel(provider)}</span>
            )}
            {application.score !== null && application.score !== undefined && (
              <span className="chip-mono">score {Math.round(application.score.normalized)}</span>
            )}
            <span className="chip-mono">app {shortId(application.id)}</span>
            <span className="text-mini text-muted">
              waiting {formatRelative(application.updated_at)}
            </span>
          </span>
        }
        actions={
          item.reason === null || item.reason === undefined ? (
            <Badge tone={tone}>{tone.label}</Badge>
          ) : (
            <Badge tone={statusTone('needs_review')}>{reviewReasonLabel(item.reason)}</Badge>
          )
        }
      />

      {expanded && (
        <CardBody className="flex flex-col gap-4">
          {answerable.length === 0 && uploads.length === 0 && (
            <p className="text-sm text-secondary">
              The pipeline recorded no open fields for this application. It stopped for the
              reason above rather than on a question, so approving it simply hands it back to
              the apply queue.
            </p>
          )}

          {answerable.map((field, index) => (
            <ReviewFieldEditor
              key={field.selector === '' ? `${field.label}-${String(index)}` : field.selector}
              index={index + 1}
              field={field}
              value={answers[field.selector] ?? ''}
              minConfidence={minConfidence}
              onChange={(value) => {
                setAnswer(field.selector, value);
              }}
            />
          ))}

          {uploads.length > 0 && (
            <div className="rounded-md border border-st-review/40 bg-st-review/[0.08] p-3">
              <p className="mb-1 flex items-center gap-1.5 text-sm font-medium text-primary">
                <AlertTriangle className="size-3.5 text-st-review" aria-hidden="true" />
                {uploads.length === 1 ? 'One file upload' : `${String(uploads.length)} file uploads`}{' '}
                could not be completed
              </p>
              <ul className="ml-5 list-disc text-sm text-secondary">
                {uploads.map((field) => (
                  <li key={field.selector}>{orDash(field.label)}</li>
                ))}
              </ul>
              <p className="mt-1 text-mini text-muted">
                Approving does not answer these. If the form requires them, finish this
                application in your browser instead.
              </p>
            </div>
          )}

          <ConsequencePair
            company={company}
            submissionAllowed={submissionAllowed}
            safetyWarning={safetyWarning}
          />

          <div className="flex items-center gap-2 border-t border-state-divider pt-3">
            <Button
              variant="danger"
              onClick={() => {
                setConfirmDismiss(true);
              }}
              trailingIcon={<Kbd keys={['Ctrl', 'X']} />}
            >
              Dismiss
            </Button>

            <span className="flex-1 font-mono text-mini tabular-nums text-muted">
              {filledCount} / {answerable.length} answered
              {missingRequired.length > 0 &&
                ` · ${String(missingRequired.length)} required still empty`}
            </span>

            <Tooltip content="Keeps your answers on this device until the app closes. Nothing is sent.">
              <Button variant="secondary" onClick={saveDraft} trailingIcon={<Kbd keys={['D']} />}>
                Save draft
              </Button>
            </Tooltip>

            <Button
              variant="primary"
              loading={resolving}
              disabled={filledCount === 0 && answerable.length > 0}
              onClick={submit}
              trailingIcon={<Kbd keys={['Ctrl', 'Enter']} />}
            >
              Approve &amp; submit
            </Button>
          </div>
        </CardBody>
      )}

      <ConfirmDialog
        open={confirmDismiss}
        onOpenChange={setConfirmDismiss}
        title={`Dismiss the ${company} application?`}
        description={
          <>
            It is marked <span className="font-mono">abandoned</span> and will not be retried.
            Nothing is sent to {company}. This cannot be undone.
            <Input
              value={note}
              onChange={(event) => {
                setNote(event.target.value);
              }}
              placeholder="Why? (optional — saved on the application)"
              aria-label="Reason for dismissing"
              className="mt-3"
            />
          </>
        }
        confirmLabel="Dismiss Application"
        onConfirm={() => {
          setConfirmDismiss(false);
          DRAFTS.delete(application.id);
          onDismiss(note.trim());
        }}
      />
    </Card>
  );
}

/**
 * The consequence pair.
 *
 * Research finding, adopted: people approve automation decisions faster and regret them more
 * when the outcome of each branch is left implicit. Both branches are stated, in the user's
 * terms, before either button is reachable — and the approve branch tells the truth about the
 * kill switch rather than the truth about the button.
 */
function ConsequencePair({
  company,
  submissionAllowed,
  safetyWarning,
}: {
  company: string;
  submissionAllowed: boolean;
  safetyWarning: string | null;
}) {
  return (
    <div className="grid gap-2 rounded-md border border-default bg-inset p-3 md:grid-cols-2">
      <Consequence label="If you approve">
        {submissionAllowed ? (
          <>
            Your answers are saved on this application and remembered, so the same question
            answers itself next time. The application returns to the apply queue and{' '}
            <strong className="font-medium text-primary">
              ApplicantOS submits it to {company}
            </strong>
            . A submission cannot be taken back.
          </>
        ) : (
          <>
            Your answers are saved on this application and remembered, so the same question
            answers itself next time. The application returns to the apply queue — but{' '}
            <strong className="font-medium text-st-review">
              {safetyWarning ?? 'submission is disabled'}
            </strong>
            , so <strong className="font-medium text-primary">nothing is sent to {company}</strong>{' '}
            until both safety switches are armed in Settings.
          </>
        )}
      </Consequence>
      <Consequence label="If you dismiss">
        Nothing is sent to {company}. The application closes as{' '}
        <span className="font-mono text-mini">abandoned</span> — not <em>failed</em>, because a
        decision is not an error, and your reliability numbers stay honest. It will not be
        retried, and the queue loses one item.
      </Consequence>
    </div>
  );
}

/** One half of the pair. */
function Consequence({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <p className="label-caps mb-1">{label}</p>
      <p className="text-sm text-secondary">{children}</p>
    </div>
  );
}

/** One open question, rendered for its field kind. */
function ReviewFieldEditor({
  index,
  field,
  value,
  minConfidence,
  onChange,
}: {
  index: number;
  field: ReviewField;
  value: string;
  minConfidence: number;
  onChange: (value: string) => void;
}) {
  const lowConfidence =
    field.confidence !== null && field.confidence !== undefined && field.confidence < minConfidence;
  const id = `review-field-${String(index)}`;

  return (
    <div
      className={cn(
        'flex flex-col gap-1.5 border-l-2 pl-3',
        lowConfidence ? 'border-l-st-review' : 'border-l-state-divider',
      )}
    >
      <div className="flex items-baseline gap-2">
        <span className="font-mono text-mini text-muted">Q{index}</span>
        <Label htmlFor={id} required={field.required} className="min-w-0 flex-1">
          {orDash(field.label)}
        </Label>
        {field.max_length !== null && field.max_length !== undefined && (
          <CharacterCounter value={value} max={field.max_length} />
        )}
      </div>

      {field.hint !== null && field.hint !== undefined && field.hint !== '' && (
        <p className="text-mini text-muted">{field.hint}</p>
      )}

      <FieldControl id={id} field={field} value={value} onChange={onChange} />

      {field.confidence !== null && field.confidence !== undefined && (
        <p className="flex items-center gap-2 text-mini">
          <span className="text-muted">The agent&apos;s own confidence in its draft:</span>
          <span
            className={cn(
              'font-mono tabular-nums',
              lowConfidence
                ? 'rounded-sm bg-st-review/[0.14] px-1 text-st-review'
                : 'text-secondary',
            )}
          >
            {field.confidence.toFixed(2)}
          </span>
          {lowConfidence && (
            <span className="text-muted">
              below your floor of {minConfidence.toFixed(2)} — which is why it stopped
            </span>
          )}
        </p>
      )}
    </div>
  );
}

/** The input itself, chosen by `FieldKind`. */
function FieldControl({
  id,
  field,
  value,
  onChange,
}: {
  id: string;
  field: ReviewField;
  value: string;
  onChange: (value: string) => void;
}) {
  const placeholder =
    field.suggested_answer === null || field.suggested_answer === undefined
      ? 'Your answer'
      : field.suggested_answer;

  if (field.kind === 'select' || field.kind === 'radio') {
    if (field.options.length === 0) {
      return (
        <Input
          id={id}
          value={value}
          placeholder={placeholder}
          onChange={(event) => {
            onChange(event.target.value);
          }}
        />
      );
    }
    if (field.kind === 'radio') {
      return (
        <RadioGroup
          value={value}
          onValueChange={onChange}
          className="flex flex-col gap-1"
          aria-labelledby={id}
        >
          {field.options.map((option) => (
            <label key={option} className="flex items-center gap-2 text-sm text-secondary">
              <Radio value={option} />
              {option}
            </label>
          ))}
        </RadioGroup>
      );
    }
    return (
      <Select value={value} onValueChange={onChange}>
        <SelectTrigger id={id} aria-label={field.label}>
          <SelectValue placeholder="Choose an answer" />
        </SelectTrigger>
        <SelectContent>
          {field.options.map((option) => (
            <SelectItem key={option} value={option}>
              {option}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    );
  }

  if (field.kind === 'checkbox') {
    return (
      <CheckboxField
        id={id}
        label={field.label === '' ? 'Yes' : field.label}
        checked={value === 'true'}
        onCheckedChange={(checked) => {
          onChange(checked === true ? 'true' : 'false');
        }}
      />
    );
  }

  if (field.kind === 'textarea' || (field.max_length ?? 0) > 160) {
    return (
      <Textarea
        id={id}
        value={value}
        autoGrow
        rows={3}
        placeholder={placeholder}
        {...(field.max_length === null || field.max_length === undefined
          ? {}
          : { maxLength: field.max_length })}
        onChange={(event) => {
          onChange(event.target.value);
        }}
      />
    );
  }

  const inputType =
    field.kind === 'email'
      ? 'email'
      : field.kind === 'number'
        ? 'number'
        : field.kind === 'date'
          ? 'date'
          : field.kind === 'url'
            ? 'url'
            : field.kind === 'phone'
              ? 'tel'
              : 'text';

  return (
    <Input
      id={id}
      type={inputType}
      value={value}
      placeholder={placeholder}
      {...(field.max_length === null || field.max_length === undefined
        ? {}
        : { maxLength: field.max_length })}
      onChange={(event) => {
        onChange(event.target.value);
      }}
    />
  );
}
