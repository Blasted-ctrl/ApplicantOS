/**
 * Onboarding (`docs/UI.md` §8.1).
 *
 * A full-window takeover — titlebar only, no sidebar, no toolbar, a 560px column — and the one
 * screen in the product where the full motion vocabulary is allowed, because it is seen once
 * per install.
 *
 * The wizard renders **blind**: `GET /onboarding/steps` describes each field well enough for
 * this component to draw it, and the server validates against the same definition it handed
 * out. That is why there is no per-step schema here. Two steps are exceptions, and both are
 * exceptions on purpose:
 *
 * **Demographics.** Every EEO field defaults to *decline to self-identify*, that option is
 * listed first, and the copy states plainly that these answers are never inferred and never
 * guessed at. This is a legal and a human matter, not a completeness one: a blank field is a
 * better outcome than a filled-in assumption, every time.
 *
 * **Safety.** Not skippable, and stated in plain language with the consequence spelled out
 * (P7). A person who finishes onboarding without understanding that both switches must be set
 * before anything is submitted has been failed by the product, not by themselves.
 */

import { motion, useReducedMotion } from 'framer-motion';
import { Check, ShieldAlert, ShieldCheck } from 'lucide-react';
import { useNavigate } from '@tanstack/react-router';
import { useCallback, useMemo, useState } from 'react';

import {
  Badge,
  Button,
  Card,
  CardBody,
  CardHeader,
  Checkbox,
  EmptyState,
  Field,
  Input,
  Kbd,
  ProgressRing,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  Switch,
  Textarea,
} from '@/components/ui';
import {
  useCompleteOnboarding,
  useOnboardingStatus,
  useOnboardingSteps,
  useReindex,
  useSettings,
  useSources,
  useSubmitOnboardingStep,
  useUpdateSettings,
} from '@/hooks';
import { motionSafe, staggerDelay, T, V, type MotionVariantSet } from '@/lib/motion';
import type { JsonObject, OnboardingField, OnboardingStep } from '@/lib/api/types';
import { cn, formatRelative, indexStatusTone, orDash, sourceKindLabel } from '@/lib/utils';
import { useSessionStore } from '@/stores/session';

/** The option value shown to a user who declines to answer a demographic question. */
const DECLINE = 'decline_to_self_identify';

/** Human copy for the decline option, whatever the server calls it. */
const DECLINE_LABEL = 'Decline to self-identify';

/** Steps this component adds after the server's, in order. */
type LocalStep = 'safety' | 'index' | 'review';

/** Read an option's stored value out of the server's loosely-typed option object. */
function optionValue(option: JsonObject, index: number): string {
  const raw = option['value'] ?? option['key'] ?? option['name'] ?? option['id'];
  return typeof raw === 'string' || typeof raw === 'number' ? String(raw) : String(index);
}

/** Read an option's label, falling back to its value. */
function optionLabel(option: JsonObject, index: number): string {
  const raw = option['label'] ?? option['title'] ?? option['name'];
  return typeof raw === 'string' ? raw : optionValue(option, index);
}

/** Whether a step asks demographic questions. */
function isDemographics(step: OnboardingStep): boolean {
  return step.key === 'demographics';
}

/** The wizard. */
export function OnboardingRoute() {
  const navigate = useNavigate();
  const reduce = useReducedMotion();
  const stepsQuery = useOnboardingSteps();
  const statusQuery = useOnboardingStatus();
  const submitStep = useSubmitOnboardingStep();
  const complete = useCompleteOnboarding();
  const settings = useSettings();
  const updateSettings = useUpdateSettings();
  const sources = useSources({ limit: 50 });
  const reindex = useReindex();
  const indexing = useSessionStore((state) => state.indexing);

  const serverSteps = useMemo(() => stepsQuery.data ?? [], [stepsQuery.data]);
  const localSteps: readonly LocalStep[] = ['safety', 'index', 'review'];
  const totalSteps = serverSteps.length + localSteps.length;

  const [index, setIndex] = useState(0);
  const [values, setValues] = useState<Record<string, Record<string, unknown>>>({});

  const currentServerStep = serverSteps[index];
  const localStep = index >= serverSteps.length ? localSteps[index - serverSteps.length] : undefined;

  const setValue = useCallback((stepKey: string, name: string, value: unknown) => {
    setValues((current) => ({
      ...current,
      [stepKey]: { ...(current[stepKey] ?? {}), [name]: value },
    }));
  }, []);

  const advance = useCallback(() => {
    setIndex((current) => Math.min(current + 1, totalSteps - 1));
  }, [totalSteps]);

  const submitCurrent = useCallback(() => {
    if (currentServerStep === undefined) {
      advance();
      return;
    }
    submitStep.mutate(
      { step: currentServerStep.key, values: values[currentServerStep.key] ?? {} },
      { onSuccess: advance },
    );
  }, [advance, currentServerStep, submitStep, values]);

  const variants = motionSafe(V.listRow, reduce);
  const progress = totalSteps === 0 ? 0 : (index + 1) / totalSteps;

  return (
    <div className="scroll-region flex min-h-0 flex-1 justify-center bg-page">
      <div className="w-[560px] py-10">
        {/* Step rail. */}
        <ol className="mb-8 flex items-center gap-2" aria-label="Setup progress">
          {Array.from({ length: totalSteps }, (_, position) => (
            <li key={position} className="flex flex-1 items-center gap-2">
              <span
                aria-current={position === index ? 'step' : undefined}
                className={cn(
                  'size-2 shrink-0 rounded-full',
                  position < index && 'bg-accent',
                  position === index && 'bg-accent-text',
                  position > index && 'bg-state-track',
                )}
              />
              {position < totalSteps - 1 && (
                <span
                  aria-hidden="true"
                  className={cn('h-px flex-1', position < index ? 'bg-accent' : 'bg-state-track')}
                />
              )}
            </li>
          ))}
        </ol>

        <p className="label-caps mb-2">
          Step {index + 1} of {totalSteps} · {Math.round(progress * 100)}%
        </p>

        {currentServerStep !== undefined && (
          <ServerStep
            step={currentServerStep}
            values={values[currentServerStep.key] ?? {}}
            onChange={(name, value) => {
              setValue(currentServerStep.key, name, value);
            }}
            variants={variants}
          />
        )}

        {localStep === 'safety' && (
          <motion.section initial={variants.initial} animate={variants.animate} transition={T.pop}>
            <h2 className="font-display text-xl font-semibold text-primary">
              Nothing is submitted until you say so
            </h2>
            <p className="mt-1 text-sm text-secondary">
              Two independent switches gate every real submission, and both start closed. This
              step is not skippable, because the whole product turns on understanding it.
            </p>

            <Card className="mt-5 border-st-review/40 bg-st-review/[0.06]">
              <CardHeader
                title={
                  <span className="inline-flex items-center gap-2">
                    <ShieldAlert className="size-4 text-st-review" aria-hidden="true" />
                    {settings.data?.is_submission_allowed === true
                      ? 'Submission is armed'
                      : 'Submission is off'}
                  </span>
                }
                subtitle="Leave both as they are and ApplicantOS will do everything except press submit."
              />
              <CardBody className="flex flex-col gap-4">
                <label className="flex items-start justify-between gap-4">
                  <span className="min-w-0">
                    <span className="block text-sm text-primary">Enable auto-apply</span>
                    <span className="block text-mini text-muted">
                      With this off, every prepared application waits in the review queue instead
                      of being sent.
                    </span>
                  </span>
                  <Switch
                    checked={settings.data?.auto_apply_enabled ?? false}
                    aria-label="Enable auto-apply"
                    onCheckedChange={(checked) => {
                      updateSettings.mutate({ auto_apply_enabled: checked });
                    }}
                  />
                </label>

                <label className="flex items-start justify-between gap-4">
                  <span className="min-w-0">
                    <span className="block text-sm text-primary">Dry run</span>
                    <span className="block text-mini text-muted">
                      While this is on, the browser fills every field, captures the screenshot,
                      and stops without submitting. It is the safe position, and it is the
                      default.
                    </span>
                  </span>
                  <Switch
                    checked={settings.data?.dry_run ?? true}
                    aria-label="Dry run"
                    onCheckedChange={(checked) => {
                      updateSettings.mutate({ dry_run: checked });
                    }}
                  />
                </label>

                <p className="rounded-md border border-default bg-inset p-3 text-mini text-secondary">
                  A submission cannot be taken back. Both switches live in Settings and the
                  titlebar shows a chip whenever either one is holding submission back, so you
                  never have to guess which mode you are in.
                </p>
              </CardBody>
            </Card>
          </motion.section>
        )}

        {localStep === 'index' && (
          <motion.section initial={variants.initial} animate={variants.animate} transition={T.pop}>
            <h2 className="font-display text-xl font-semibold text-primary">
              Reading what you pointed us at
            </h2>
            <p className="mt-1 text-sm text-secondary">
              Indexing runs in the background — you can continue while it works. Every résumé
              bullet the system will ever write has to trace back to something it read here.
            </p>

            <div className="mt-5 flex flex-col gap-2">
              {(sources.data?.items ?? []).length === 0 ? (
                <EmptyState
                  title="No sources yet"
                  description="You can add them later from the Knowledge screen. Without at least one, résumés cannot be generated, because there would be no facts to build them from."
                />
              ) : (
                (sources.data?.items ?? []).map((source, position) => {
                  const tone = indexStatusTone(source.index_status);
                  const active = indexing !== null && indexing.sourceId === source.id;
                  return (
                    <motion.div
                      key={source.id}
                      initial={variants.initial}
                      animate={variants.animate}
                      transition={{ ...T.pop, delay: staggerDelay(position) }}
                      className="flex items-center gap-3 rounded-md border border-default bg-surface p-3"
                    >
                      <span className="min-w-0 flex-1">
                        <span className="block truncate text-sm text-primary">
                          {orDash(source.label ?? sourceKindLabel(source.kind))}
                        </span>
                        <span className="block truncate font-mono text-micro tracking-normal text-muted">
                          {source.uri}
                        </span>
                      </span>
                      {active && (
                        <ProgressRing
                          value={indexing.progress ?? 0}
                          label={`Indexing ${source.uri}`}
                        />
                      )}
                      <Badge tone={tone}>{tone.label}</Badge>
                      {source.last_indexed_at != null && (
                        <span className="shrink-0 font-mono text-micro tracking-normal text-muted">
                          {formatRelative(source.last_indexed_at)}
                        </span>
                      )}
                    </motion.div>
                  );
                })
              )}
            </div>

            <Button
              variant="secondary"
              className="mt-4"
              loading={reindex.isPending}
              disabled={(sources.data?.items ?? []).length === 0}
              onClick={() => {
                reindex.mutate({});
              }}
            >
              Index everything now
            </Button>
          </motion.section>
        )}

        {localStep === 'review' && (
          <motion.section initial={variants.initial} animate={variants.animate} transition={T.pop}>
            <h2 className="font-display text-xl font-semibold text-primary">Ready</h2>
            <p className="mt-1 text-sm text-secondary">
              Finishing enqueues the first index of everything you connected. You can change any
              answer later from Settings.
            </p>

            <ul className="mt-5 flex flex-col gap-2">
              {serverSteps.map((step) => (
                <li
                  key={step.key}
                  className="flex items-center gap-3 rounded-md border border-default bg-surface p-3"
                >
                  <Check
                    className={cn(
                      'size-4 shrink-0',
                      step.complete ? 'text-st-success' : 'text-disabled',
                    )}
                    aria-hidden="true"
                  />
                  <span className="min-w-0 flex-1 truncate text-sm text-primary">{step.title}</span>
                  <span className="shrink-0 text-mini text-muted">
                    {step.complete ? 'answered' : step.required ? 'still required' : 'skipped'}
                  </span>
                </li>
              ))}
              <li className="flex items-center gap-3 rounded-md border border-default bg-surface p-3">
                {settings.data?.is_submission_allowed === true ? (
                  <ShieldCheck className="size-4 shrink-0 text-st-success" aria-hidden="true" />
                ) : (
                  <ShieldAlert className="size-4 shrink-0 text-st-review" aria-hidden="true" />
                )}
                <span className="min-w-0 flex-1 text-sm text-primary">
                  {settings.data?.is_submission_allowed === true
                    ? 'Submission is armed — runs will send real applications.'
                    : 'Submission is off — nothing will be sent until you arm both switches.'}
                </span>
              </li>
            </ul>
          </motion.section>
        )}

        {/* Footer. */}
        <div className="mt-8 flex items-center justify-between gap-3">
          <Button
            variant="ghost"
            disabled={index === 0}
            onClick={() => {
              setIndex((current) => Math.max(0, current - 1));
            }}
          >
            Back
          </Button>

          <div className="flex items-center gap-2">
            {currentServerStep !== undefined && !currentServerStep.required && (
              <Button variant="secondary" onClick={advance}>
                Skip for now
              </Button>
            )}

            {localStep === 'review' ? (
              <Button
                variant="primary"
                loading={complete.isPending}
                trailingIcon={<Kbd keys={['Ctrl', 'Enter']} />}
                onClick={() => {
                  complete.mutate(undefined, {
                    onSuccess: () => {
                      void navigate({ to: '/' });
                    },
                  });
                }}
              >
                Finish setup
              </Button>
            ) : (
              <Button
                variant="primary"
                loading={submitStep.isPending}
                trailingIcon={<Kbd keys={['Ctrl', 'Enter']} />}
                onClick={submitCurrent}
              >
                Continue
              </Button>
            )}
          </div>
        </div>

        {statusQuery.data?.complete === true && index < totalSteps - 1 && (
          <p className="mt-4 text-mini text-muted">
            You have already completed setup. Anything you change here is saved as you go.
          </p>
        )}
      </div>
    </div>
  );
}

/** One server-described step. */
function ServerStep({
  step,
  values,
  onChange,
  variants,
}: {
  step: OnboardingStep;
  values: Record<string, unknown>;
  onChange: (name: string, value: unknown) => void;
  variants: MotionVariantSet;
}) {
  const demographic = isDemographics(step);

  return (
    <motion.section initial={variants.initial} animate={variants.animate} transition={T.pop}>
      <h2 className="font-display text-xl font-semibold text-primary">{step.title}</h2>
      {step.description != null && step.description !== '' && (
        <p className="mt-1 text-sm text-secondary">{step.description}</p>
      )}

      {demographic && (
        <p className="mt-4 rounded-md border border-default bg-inset p-3 text-sm text-secondary">
          <strong className="font-medium text-primary">
            These answers are never inferred and never guessed at.
          </strong>{' '}
          Every one of them defaults to <em>decline to self-identify</em>, and leaving them that
          way is a complete answer that employers accept. ApplicantOS will submit exactly what
          you put here and nothing else.
        </p>
      )}

      <div className="mt-5 flex flex-col gap-4">
        {step.fields.map((field, position) => (
          <motion.div
            key={field.name}
            initial={variants.initial}
            animate={variants.animate}
            transition={{ ...T.pop, delay: staggerDelay(position) }}
          >
            <StepField
              field={field}
              demographic={demographic}
              value={values[field.name]}
              onChange={(value) => {
                onChange(field.name, value);
              }}
            />
          </motion.div>
        ))}
      </div>
    </motion.section>
  );
}

/** One field, drawn from its server description. */
function StepField({
  field,
  demographic,
  value,
  onChange,
}: {
  field: OnboardingField;
  demographic: boolean;
  value: unknown;
  onChange: (value: unknown) => void;
}) {
  const id = `onboarding-${field.name}`;
  const text = typeof value === 'string' ? value : '';

  if (field.kind === 'select' || field.kind === 'radio') {
    const options = field.options.map((option, position) => ({
      value: optionValue(option, position),
      label: optionLabel(option, position),
    }));
    // Decline first and selected by default — the obvious answer, not the buried one.
    const withDecline = demographic
      ? [
          { value: DECLINE, label: DECLINE_LABEL },
          ...options.filter((option) => option.value !== DECLINE),
        ]
      : options;
    const selected = typeof value === 'string' ? value : demographic ? DECLINE : '';

    return (
      <Field
        label={field.label}
        {...(field.help == null ? {} : { help: field.help })}
        required={field.required && !demographic}
        htmlFor={id}
      >
        <Select
          value={selected}
          onValueChange={(next) => {
            onChange(next);
          }}
        >
          <SelectTrigger id={id} aria-label={field.label}>
            <SelectValue placeholder={demographic ? DECLINE_LABEL : 'Choose one'} />
          </SelectTrigger>
          <SelectContent>
            {withDecline.map((option) => (
              <SelectItem key={option.value} value={option.value}>
                {option.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </Field>
    );
  }

  if (field.kind === 'checkbox') {
    return (
      <label className="flex items-start gap-3">
        <Checkbox
          id={id}
          checked={value === true}
          onCheckedChange={(checked) => {
            onChange(checked === true);
          }}
        />
        <span className="text-sm">
          <span className="block text-primary">{field.label}</span>
          {field.help != null && <span className="block text-mini text-muted">{field.help}</span>}
        </span>
      </label>
    );
  }

  if (field.kind === 'textarea' || field.kind === 'multiselect') {
    return (
      <Field
        label={field.label}
        help={field.help ?? (field.kind === 'multiselect' ? 'Comma separated.' : undefined)}
        required={field.required}
        htmlFor={id}
      >
        <Textarea
          id={id}
          autoGrow
          rows={3}
          value={text}
          {...(field.placeholder == null ? {} : { placeholder: field.placeholder })}
          onChange={(event) => {
            onChange(
              field.kind === 'multiselect'
                ? event.target.value
                    .split(',')
                    .map((part) => part.trim())
                    .filter((part) => part !== '')
                : event.target.value,
            );
          }}
        />
      </Field>
    );
  }

  const type =
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
    <Field
      label={field.label}
      {...(field.help == null ? {} : { help: field.help })}
      required={field.required}
      htmlFor={id}
    >
      <Input
        id={id}
        type={type}
        value={text}
        {...(field.placeholder == null ? {} : { placeholder: field.placeholder })}
        onChange={(event) => {
          onChange(event.target.value);
        }}
      />
    </Field>
  );
}
