/**
 * The three dialogs that start, stop and feed a run.
 *
 * All three are *decisions*, which is why they are Dialogs rather than Sheets (§7.12), and all
 * three touch the outside world, which is why none of them is optimistic (§10.9).
 *
 * **`Start a run` restates the safety envelope before it starts anything.** Both switches and
 * what they currently mean, in a sentence, above the button. A user who arms auto-apply for a
 * single run does so here with the consequence in front of them rather than three screens away
 * in Settings — and the per-run override is explicitly temporary, which the copy says.
 *
 * **`Stop the run` is in the `Ctrl` namespace and confirms** (§9.1, §9.2). It interrupts
 * browser automation mid-flight; the confirmation names the run and what stopping costs.
 */

import { useMemo, useState } from 'react';

import {
  Button,
  ConfirmDialog,
  Dialog,
  DialogClose,
  DialogContent,
  Field,
  Input,
  Kbd,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  Switch,
} from '@/components/ui';
import { usePlugins, useResumes } from '@/hooks';
import {
  ATS_PROVIDER_NAMES,
  AUTO_APPLY_PROVIDERS,
  type ATSProviderName,
  type DiscoverRequest,
  type SessionRead,
  type SessionStartRequest,
} from '@/lib/api/types';
import { cn, formatRelative, providerLabel } from '@/lib/utils';

/**
 * The picker's "use my default" option.
 *
 * A sentinel rather than an empty string, because Radix treats `''` as "no value" and would
 * render the placeholder instead of this choice — and "use my default" is a real answer the
 * user should see selected, not an absence.
 */
const DEFAULT_RESUME_VALUE = '__default__';

/** Split a comma- or newline-separated field into a clean list. */
function toList(value: string): string[] {
  return value
    .split(/[\n,]/)
    .map((part) => part.trim())
    .filter((part) => part !== '');
}

/** Props for {@link StartRunDialog}. */
export interface StartRunDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** `settings.auto_apply_enabled`. */
  autoApplyEnabled: boolean;
  /** `settings.dry_run`. */
  dryRun: boolean;
  onStart: (request: SessionStartRequest) => void;
  starting?: boolean;
}

/** Configure and start one automation run. */
export function StartRunDialog({
  open,
  onOpenChange,
  autoApplyEnabled,
  dryRun,
  onStart,
  starting = false,
}: StartRunDialogProps) {
  const [overrideSafety, setOverrideSafety] = useState(false);
  const [autoApply, setAutoApply] = useState(autoApplyEnabled);
  const [runDryRun, setRunDryRun] = useState(dryRun);
  const [maxApplications, setMaxApplications] = useState('');
  const [matchThreshold, setMatchThreshold] = useState('');
  const [resumeId, setResumeId] = useState(DEFAULT_RESUME_VALUE);
  const { data: resumes } = useResumes();
  const variants = resumes?.items ?? [];

  const effectiveAuto = overrideSafety ? autoApply : autoApplyEnabled;
  const effectiveDry = overrideSafety ? runDryRun : dryRun;
  const willSubmit = effectiveAuto && !effectiveDry;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        open={open}
        size="md"
        title="Start a run"
        description="Discovery, scoring, tailoring and — if both switches allow it — submission."
        footer={
          <>
            <DialogClose asChild>
              <Button variant="secondary">Cancel</Button>
            </DialogClose>
            <Button
              variant="primary"
              loading={starting}
              trailingIcon={<Kbd keys={['Ctrl', 'Enter']} />}
              onClick={() => {
                const cap = Number.parseInt(maxApplications, 10);
                const floor = Number.parseInt(matchThreshold, 10);
                onStart({
                  trigger: 'manual',
                  auto_apply: overrideSafety ? autoApply : null,
                  dry_run: overrideSafety ? runDryRun : null,
                  max_applications: Number.isFinite(cap) && cap > 0 ? cap : null,
                  match_threshold:
                    Number.isFinite(floor) && floor > 0 && floor <= 100 ? floor : null,
                  resume_id: resumeId === DEFAULT_RESUME_VALUE ? null : resumeId,
                });
              }}
            >
              Start the run
            </Button>
          </>
        }
      >
        <div className="flex flex-col gap-4">
          <p
            className={cn(
              'rounded-md border p-3 text-sm',
              willSubmit
                ? 'border-st-success/40 bg-st-success/[0.08] text-primary'
                : 'border-st-review/40 bg-st-review/[0.08] text-primary',
            )}
          >
            {willSubmit ? (
              <>
                This run <strong className="font-medium">will submit real applications</strong> to
                real employers. Everything it sends is recorded with a screenshot, but a
                submission cannot be taken back.
              </>
            ) : (
              <>
                This run will discover, score and tailor, but{' '}
                <strong className="font-medium">nothing will be submitted</strong> —{' '}
                {effectiveDry ? 'dry run is on' : 'auto-apply is off'}. Anything it would have
                sent lands in the review queue instead.
              </>
            )}
          </p>

          {/*
            Only shown when there is a choice to make. A user with one variant is told
            nothing by a dropdown containing one item, and a first run should not have to
            answer a question that has one answer.
          */}
          {variants.length > 1 && (
            <Field
              label="Résumé"
              help="Which variant this run tailors from."
              htmlFor="run-resume"
            >
              <Select value={resumeId} onValueChange={setResumeId}>
                <SelectTrigger id="run-resume" aria-label="Résumé variant">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value={DEFAULT_RESUME_VALUE}>Use my default</SelectItem>
                  {variants.map((variant) => (
                    <SelectItem key={variant.id} value={variant.id}>
                      {variant.name}
                      {variant.is_default ? ' (default)' : ''}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </Field>
          )}

          {/*
            Both fields narrow and neither widens: the server takes the *lower* cap and the
            *higher* threshold of what is asked here and what Settings holds. Saying so in
            the help text matters, because a field that silently ignored you would be worse
            than no field at all.
          */}
          <div className="grid grid-cols-2 gap-3">
            <Field
              label="Stop after"
              help="Lower of this and the caps in Settings."
              htmlFor="run-max"
            >
              <Input
                id="run-max"
                type="number"
                min={1}
                mono
                value={maxApplications}
                placeholder="applications"
                onChange={(event) => {
                  setMaxApplications(event.target.value);
                }}
              />
            </Field>
            <Field
              label="Minimum match"
              help="Higher of this and the floor in Settings."
              htmlFor="run-threshold"
            >
              <Input
                id="run-threshold"
                type="number"
                min={1}
                max={100}
                mono
                value={matchThreshold}
                placeholder="score out of 100"
                onChange={(event) => {
                  setMatchThreshold(event.target.value);
                }}
              />
            </Field>
          </div>

          <label className="flex items-start gap-3">
            <Switch
              checked={overrideSafety}
              onCheckedChange={(checked) => {
                setOverrideSafety(checked);
                setAutoApply(autoApplyEnabled);
                setRunDryRun(dryRun);
              }}
            />
            <span className="text-sm">
              <span className="font-medium text-primary">Override the switches for this run</span>
              <span className="block text-mini text-muted">
                Applies to this run only. Your stored settings are untouched.
              </span>
            </span>
          </label>

          {overrideSafety && (
            <div className="flex flex-col gap-3 rounded-md border border-default bg-inset p-3">
              <label className="flex items-center justify-between gap-3 text-sm text-secondary">
                <span>
                  Enable auto-apply
                  <span className="ml-2 font-mono text-mini text-muted">auto_apply</span>
                </span>
                <Switch checked={autoApply} onCheckedChange={setAutoApply} />
              </label>
              <label className="flex items-center justify-between gap-3 text-sm text-secondary">
                <span>
                  Dry run — never submits
                  <span className="ml-2 font-mono text-mini text-muted">dry_run</span>
                </span>
                <Switch checked={runDryRun} onCheckedChange={setRunDryRun} />
              </label>
            </div>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}

/** Props for {@link DiscoverDialog}. */
export interface DiscoverDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onDiscover: (request: DiscoverRequest) => void;
  discovering?: boolean;
}

/**
 * One manual discovery pass.
 *
 * The provider list is annotated with each one's automation posture, because LinkedIn and
 * Workday are **discovery-only** by contract — LinkedIn's ToS prohibits automated submission
 * and Workday's account-gated flow routes to review by design (golden rule #10). Selecting
 * them is legitimate and useful; believing they will apply for you is not.
 */
export function DiscoverDialog({
  open,
  onOpenChange,
  onDiscover,
  discovering = false,
}: DiscoverDialogProps) {
  const { data: plugins } = usePlugins({ kind: 'provider' });
  const [selected, setSelected] = useState<readonly ATSProviderName[]>([]);
  const [keywords, setKeywords] = useState('');
  const [locations, setLocations] = useState('');
  const [remoteOnly, setRemoteOnly] = useState(false);
  const [postedWithin, setPostedWithin] = useState('14');
  const [limit, setLimit] = useState('50');

  const available = useMemo<readonly ATSProviderName[]>(() => {
    const registered = new Set((plugins?.items ?? []).map((plugin) => plugin.name));
    const known = ATS_PROVIDER_NAMES.filter((name) => name !== 'manual');
    const matched = known.filter((name) => registered.has(name));
    return matched.length > 0 ? matched : known;
  }, [plugins]);

  const toggle = (provider: ATSProviderName) => {
    setSelected((current) =>
      current.includes(provider)
        ? current.filter((name) => name !== provider)
        : [...current, provider],
    );
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        open={open}
        size="md"
        title="Discover postings"
        description="One pass over the selected providers. Results arrive as they are found."
        footer={
          <>
            <DialogClose asChild>
              <Button variant="secondary">Cancel</Button>
            </DialogClose>
            <Button
              variant="primary"
              loading={discovering}
              trailingIcon={<Kbd keys={['Ctrl', 'Enter']} />}
              onClick={() => {
                const days = Number.parseInt(postedWithin, 10);
                const cap = Number.parseInt(limit, 10);
                onDiscover({
                  ...(selected.length > 0 ? { providers: [...selected] } : {}),
                  keywords: toList(keywords),
                  locations: toList(locations),
                  remote_only: remoteOnly,
                  posted_within_days: Number.isFinite(days) && days > 0 ? days : null,
                  ...(Number.isFinite(cap) && cap > 0 ? { limit: cap } : {}),
                });
              }}
            >
              Discover
            </Button>
          </>
        }
      >
        <div className="flex flex-col gap-4">
          <fieldset>
            <legend className="label-caps mb-2">Providers</legend>
            <div className="flex flex-wrap gap-2">
              {available.map((provider) => {
                const active = selected.includes(provider);
                const applies = AUTO_APPLY_PROVIDERS.has(provider);
                return (
                  <Button
                    key={provider}
                    variant="chip"
                    size="sm"
                    aria-pressed={active}
                    onClick={() => {
                      toggle(provider);
                    }}
                    className={cn(
                      active && 'border-accent-border bg-accent-subtle text-accent-text',
                    )}
                  >
                    {providerLabel(provider)}
                    {!applies && (
                      <span className="ml-1 text-micro tracking-normal text-st-review">
                        discovery only
                      </span>
                    )}
                  </Button>
                );
              })}
            </div>
            <p className="mt-1.5 text-mini text-muted">
              Nothing selected means every enabled provider. LinkedIn and Workday can be searched
              but never submitted to — their terms and their flows forbid it, so anything matched
              there routes to manual review.
            </p>
          </fieldset>

          <Field label="Keywords" help="Comma separated." htmlFor="discover-keywords">
            <Input
              id="discover-keywords"
              value={keywords}
              placeholder="staff engineer, distributed systems"
              onChange={(event) => {
                setKeywords(event.target.value);
              }}
            />
          </Field>

          <Field label="Locations" help="Comma separated." htmlFor="discover-locations">
            <Input
              id="discover-locations"
              value={locations}
              placeholder="Remote, Berlin, London"
              onChange={(event) => {
                setLocations(event.target.value);
              }}
            />
          </Field>

          <div className="grid grid-cols-2 gap-3">
            <Field label="Posted within" htmlFor="discover-days">
              <Input
                id="discover-days"
                type="number"
                min={1}
                mono
                value={postedWithin}
                onChange={(event) => {
                  setPostedWithin(event.target.value);
                }}
              />
            </Field>
            <Field label="Maximum results" htmlFor="discover-limit">
              <Input
                id="discover-limit"
                type="number"
                min={1}
                mono
                value={limit}
                onChange={(event) => {
                  setLimit(event.target.value);
                }}
              />
            </Field>
          </div>

          <label className="flex items-center justify-between gap-3 text-sm text-secondary">
            <span>Remote only</span>
            <Switch checked={remoteOnly} onCheckedChange={setRemoteOnly} />
          </label>
        </div>
      </DialogContent>
    </Dialog>
  );
}

/** Props for {@link StopRunDialog}. */
export interface StopRunDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  session: SessionRead | null;
  onStop: () => void;
  stopping?: boolean;
}

/** The confirmation `Ctrl+S` must show. */
export function StopRunDialog({
  open,
  onOpenChange,
  session,
  onStop,
  stopping = false,
}: StopRunDialogProps) {
  return (
    <ConfirmDialog
      open={open}
      onOpenChange={onOpenChange}
      title="Stop the running run?"
      description={
        session === null ? (
          'No run is executing.'
        ) : (
          <>
            The run started {formatRelative(session.started_at)} and has completed{' '}
            <span className="font-mono">{session.applications_completed}</span> applications.
            Stopping ends it now; anything mid-submission finishes or lands in the review queue,
            and nothing already sent is affected.
          </>
        )
      }
      confirmLabel="Stop the Run"
      onConfirm={onStop}
      loading={stopping}
    />
  );
}
