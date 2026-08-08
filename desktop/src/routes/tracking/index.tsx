/**
 * Status sync (`docs/CONTRACTS.md` §17).
 *
 * The subsystem that closes the loop: a rejection finds its own way into the database whether
 * or not you open the app. §17.8 makes the privacy posture non-negotiable, and an invisible
 * guarantee is worth nothing — so this screen states all three of them where the user can
 * read them, next to the button that grants the access:
 *
 * - **Read-only, always.** Nothing here can send, delete, move or re-flag a message.
 * - **Credentials never touch the database.** The secret goes straight to the OS keychain and
 *   is never echoed back, which is why an empty secret field means "leave unchanged".
 * - **Minimal retention.** A signal keeps a subject and a snippet capped at 500 characters.
 *   There is no full message body to show, and this screen does not imply there is.
 *
 * Resolving a signal is the one status change a human authorises on the strength of an email,
 * so it is never optimistic and the server's answer is what lands in the cache.
 */

import { Mail, Plus, RotateCw, Trash2 } from 'lucide-react';
import { useState } from 'react';

import { Page, ResultCount, SectionHeading, ToolbarSpacer } from '@/components/page';
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
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  Sheet,
  SheetContent,
  StatTile,
  Tooltip,
} from '@/components/ui';
import {
  useApplications,
  useCreateEmailAccount,
  useDeleteEmailAccount,
  useDismissSignal,
  useEmailAccounts,
  useResolveSignal,
  useRunSync,
  useSignals,
  useTestEmailAccount,
  useTrackingStats,
} from '@/hooks';
import {
  MAIL_PROVIDERS,
  SIGNAL_KIND_TO_STATUS,
  type ApplicationStatus,
  type EmailAccountRead,
  type MailProvider,
} from '@/lib/api/types';
import {
  cn,
  formatRelative,
  orDash,
  signalKindTone,
  statusLabel,
  statusTone,
} from '@/lib/utils';

/** The status-sync screen. */
export function TrackingRoute() {
  const accounts = useEmailAccounts();
  const stats = useTrackingStats();
  const signals = useSignals({ needs_review: true, limit: 50 });
  const applications = useApplications({ limit: 100 });

  const createAccount = useCreateEmailAccount();
  const deleteAccount = useDeleteEmailAccount();
  const testAccount = useTestEmailAccount();
  const runSync = useRunSync();
  const resolveSignal = useResolveSignal();
  const dismissSignal = useDismissSignal();

  const [addOpen, setAddOpen] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState<EmailAccountRead | null>(null);
  const [provider, setProvider] = useState<MailProvider>('gmail');
  const [address, setAddress] = useState('');
  const [secret, setSecret] = useState('');
  const [folders, setFolders] = useState('INBOX');
  const [bindings, setBindings] = useState<Record<string, string>>({});

  const rows = accounts.data ?? [];
  const pending = signals.data?.items ?? [];
  const tracking = stats.data;

  return (
    <Page
      title="Status sync"
      subtitle={
        tracking === undefined
          ? 'Mailbox state loads from the local cache.'
          : `${String(tracking.accounts_enabled)}/${String(tracking.accounts)} mailboxes connected · ${String(tracking.signals_total)} signals · ${String(tracking.signals_pending_review)} awaiting you${tracking.last_sync_at == null ? '' : ` · synced ${formatRelative(tracking.last_sync_at)}`}`
      }
      busy={signals.isFetching || accounts.isFetching}
      actions={
        <>
          <Button
            variant="secondary"
            leadingIcon={<RotateCw aria-hidden="true" />}
            loading={runSync.isPending}
            disabled={rows.length === 0}
            onClick={() => {
              runSync.mutate({});
            }}
          >
            Sync now
          </Button>
          <Button
            variant="primary"
            leadingIcon={<Plus aria-hidden="true" />}
            onClick={() => {
              setAddOpen(true);
            }}
          >
            Connect a mailbox
          </Button>
        </>
      }
      toolbar={
        <>
          <ToolbarSpacer />
          <ResultCount count={pending.length} total={tracking?.signals_total ?? 0} noun="signals" />
        </>
      }
    >
      <div className="flex flex-col gap-4">
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
          <StatTile label="Mailboxes" value={tracking?.accounts ?? 0} />
          <StatTile label="Signals seen" value={tracking?.signals_total ?? 0} />
          <StatTile label="Applied automatically" value={tracking?.signals_applied ?? 0} />
          <StatTile
            label="Awaiting you"
            value={tracking?.signals_pending_review ?? 0}
            upIsGood={false}
          />
        </div>

        <Card>
          <CardHeader
            title="How this works"
            subtitle="Three guarantees, stated where the access is granted"
          />
          <CardBody>
            <ul className="ml-4 list-disc text-sm text-secondary">
              <li>
                <strong className="font-medium text-primary">Read-only.</strong> ApplicantOS can
                read message headers and a short snippet. It cannot send, delete, move or
                re-flag anything.
              </li>
              <li>
                <strong className="font-medium text-primary">
                  Credentials never touch the database.
                </strong>{' '}
                Your secret is handed to the operating system keychain and is never returned by
                the API — which is why the field below is blank when editing.
              </li>
              <li>
                <strong className="font-medium text-primary">Minimal retention.</strong> A signal
                stores the sender, the subject and a snippet capped at 500 characters. Full
                message bodies are never persisted, so there is nothing here to show you.
              </li>
            </ul>
          </CardBody>
        </Card>

        <section>
          <SectionHeading>Connected mailboxes</SectionHeading>
          {rows.length === 0 ? (
            <EmptyState
              icon={Mail}
              title="No mailbox connected"
              description="Without one, application outcomes have to be recorded by hand. With one, a rejection updates itself."
              primaryAction={{
                label: 'Connect a mailbox',
                onClick: () => {
                  setAddOpen(true);
                },
              }}
            />
          ) : (
            <ul className="flex flex-col">
              {rows.map((account) => (
                <li
                  key={account.id}
                  className="group flex items-center gap-3 border-b border-state-divider py-2 last:border-b-0"
                >
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-sm text-primary">{account.address}</span>
                    <span className="block truncate font-mono text-micro tracking-normal text-muted">
                      {account.provider} · {account.folders.join(', ')}
                      {account.last_sync_at == null
                        ? ' · never synced'
                        : ` · synced ${formatRelative(account.last_sync_at)}`}
                    </span>
                    {account.last_error != null && account.last_error !== '' && (
                      <span className="block text-micro tracking-normal text-st-danger">
                        {account.last_error}
                      </span>
                    )}
                  </span>
                  <Badge tone={statusTone(account.connected ? 'confirmed' : 'failed')}>
                    {account.connected ? 'Connected' : 'Credential missing'}
                  </Badge>
                  <span className="flex shrink-0 items-center gap-0.5 opacity-0 transition-opacity duration-[140ms] group-hover:opacity-100 group-focus-within:opacity-100">
                    <Tooltip content="Read-only connection probe">
                      <Button
                        variant="ghost"
                        size="sm"
                        icon
                        aria-label={`Test ${account.address}`}
                        loading={testAccount.isPending}
                        onClick={() => {
                          testAccount.mutate(account.id);
                        }}
                      >
                        <RotateCw aria-hidden="true" />
                      </Button>
                    </Tooltip>
                    <Tooltip content="Disconnect and forget the credential">
                      <Button
                        variant="ghost"
                        size="sm"
                        icon
                        aria-label={`Disconnect ${account.address}`}
                        onClick={() => {
                          setConfirmDelete(account);
                        }}
                      >
                        <Trash2 aria-hidden="true" />
                      </Button>
                    </Tooltip>
                  </span>
                </li>
              ))}
            </ul>
          )}
        </section>

        <section>
          <SectionHeading>Signals awaiting a decision</SectionHeading>
          {pending.length === 0 ? (
            <EmptyState
              title="Nothing ambiguous"
              description="Signals that match an application confidently are applied on their own. Only the ones that could not be matched appear here."
            />
          ) : (
            <ul className="flex flex-col gap-2">
              {pending.map((signal) => {
                const tone = signalKindTone(signal.kind);
                const implied = SIGNAL_KIND_TO_STATUS[signal.kind];
                const bound = bindings[signal.id] ?? '';
                return (
                  <li key={signal.id}>
                    <Card>
                      <CardHeader
                        title={signal.subject === '' ? '(no subject)' : signal.subject}
                        subtitle={
                          <span className="font-mono text-micro tracking-normal">
                            {signal.sender} · {formatRelative(signal.received_at)} · confidence{' '}
                            {signal.confidence.toFixed(2)}
                          </span>
                        }
                        actions={<Badge tone={tone}>{tone.label}</Badge>}
                      />
                      <CardBody className="flex flex-col gap-3">
                        <p className="text-sm text-secondary">{orDash(signal.snippet)}</p>

                        {implied === null ? (
                          <p className="text-mini text-muted">
                            This kind of message says nothing about an outcome, so no status
                            change is offered. Dismissing it simply takes it off the queue.
                          </p>
                        ) : (
                          <div className="flex flex-wrap items-end gap-2">
                            <Field
                              label="Which application does this belong to?"
                              className="min-w-[280px] flex-1"
                              htmlFor={`signal-${signal.id}`}
                            >
                              <Select
                                value={bound}
                                onValueChange={(value) => {
                                  setBindings((current) => ({ ...current, [signal.id]: value }));
                                }}
                              >
                                <SelectTrigger
                                  id={`signal-${signal.id}`}
                                  aria-label="Application to bind this signal to"
                                >
                                  <SelectValue placeholder="Choose an application" />
                                </SelectTrigger>
                                <SelectContent>
                                  {(applications.data?.items ?? []).map((application) => (
                                    <SelectItem key={application.id} value={application.id}>
                                      {orDash(application.company?.name)} ·{' '}
                                      {orDash(application.posting?.title)}
                                    </SelectItem>
                                  ))}
                                </SelectContent>
                              </Select>
                            </Field>

                            <Button
                              variant="primary"
                              disabled={bound === ''}
                              loading={resolveSignal.isPending}
                              onClick={() => {
                                resolveSignal.mutate({
                                  id: signal.id,
                                  body: {
                                    application_id: bound,
                                    status: implied as ApplicationStatus,
                                  },
                                });
                              }}
                            >
                              Mark {statusLabel(implied).toLowerCase()}
                            </Button>
                          </div>
                        )}

                        <div className="flex items-center gap-2">
                          <Button
                            variant="ghost"
                            size="sm"
                            onClick={() => {
                              dismissSignal.mutate(signal.id);
                            }}
                          >
                            Dismiss this signal
                          </Button>
                          <span className="text-mini text-muted">
                            Dismissing changes no application. It only removes the message from
                            this queue.
                          </span>
                        </div>
                      </CardBody>
                    </Card>
                  </li>
                );
              })}
            </ul>
          )}
        </section>
      </div>

      <Sheet open={addOpen} onOpenChange={setAddOpen}>
        <SheetContent
          open={addOpen}
          onOpenChange={setAddOpen}
          title="Connect a mailbox"
          description="Read-only. The secret goes to your operating system keychain, never to the database."
          footer={
            <Button
              variant="primary"
              loading={createAccount.isPending}
              disabled={address.trim() === ''}
              onClick={() => {
                createAccount.mutate(
                  {
                    provider,
                    address: address.trim(),
                    ...(secret === '' ? {} : { secret }),
                    folders: folders
                      .split(',')
                      .map((folder) => folder.trim())
                      .filter((folder) => folder !== ''),
                  },
                  {
                    onSuccess: () => {
                      setAddOpen(false);
                      setAddress('');
                      setSecret('');
                    },
                  },
                );
              }}
            >
              Connect
            </Button>
          }
        >
          <div className="flex flex-col gap-4">
            <Field label="Provider" htmlFor="mailbox-provider">
              <Select
                value={provider}
                onValueChange={(value) => {
                  setProvider(value as MailProvider);
                }}
              >
                <SelectTrigger id="mailbox-provider" aria-label="Mail provider">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {MAIL_PROVIDERS.map((option) => (
                    <SelectItem key={option} value={option}>
                      {option}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </Field>

            <Field label="Address" htmlFor="mailbox-address">
              <Input
                id="mailbox-address"
                type="email"
                mono
                value={address}
                placeholder="you@example.com"
                onChange={(event) => {
                  setAddress(event.target.value);
                }}
              />
            </Field>

            <Field
              label="App password or token"
              help="Stored in the OS keychain and never returned by the API. Leave blank to keep an existing one."
              htmlFor="mailbox-secret"
            >
              <Input
                id="mailbox-secret"
                type="password"
                mono
                value={secret}
                autoComplete="off"
                onChange={(event) => {
                  setSecret(event.target.value);
                }}
              />
            </Field>

            <Field label="Folders" help="Comma separated." htmlFor="mailbox-folders">
              <Input
                id="mailbox-folders"
                mono
                value={folders}
                onChange={(event) => {
                  setFolders(event.target.value);
                }}
              />
            </Field>

            <p className={cn('rounded-md border border-default bg-inset p-3 text-mini text-muted')}>
              ApplicantOS reads message metadata to recognise rejections, interview invitations
              and offers. It never sends mail, never deletes anything, and never stores a full
              message body.
            </p>
          </div>
        </SheetContent>
      </Sheet>

      <ConfirmDialog
        open={confirmDelete !== null}
        onOpenChange={(open) => {
          if (!open) setConfirmDelete(null);
        }}
        title="Disconnect this mailbox?"
        description={
          <>
            The stored credential and the sync cursor are deleted. Signals already recorded stay,
            and the applications they updated are unaffected.
          </>
        }
        confirmLabel="Disconnect"
        loading={deleteAccount.isPending}
        onConfirm={() => {
          const target = confirmDelete;
          setConfirmDelete(null);
          if (target !== null) deleteAccount.mutate(target.id);
        }}
      />
    </Page>
  );
}
