/**
 * FileDropzone — drag-or-browse upload (`docs/UI.md` §8.1).
 *
 * The wizard's `master_resume` step declares a field of kind `file`, and until this existed
 * the generic renderer fell through to a text input: the app asked the user to *type a
 * UUID*, which no user has. So the one screen whose entire purpose is "give us your resume"
 * had no way to give it a resume.
 *
 * Three things this component insists on:
 *
 * **A real `<input type="file">` does the work**, positioned invisibly over the whole zone
 * rather than hidden and clicked from JavaScript. That keeps keyboard focus, the native file
 * picker, the OS accessibility tree, and drag-and-drop all working without a line of code
 * imitating any of them — and it is why the drop target needs no `onDragOver` handler to
 * beat the browser's default of navigating away from the app.
 *
 * **The upload is owned here, not by the caller.** The value a caller gets back is the
 * catalogue id, because that is the only thing the API accepts downstream. A caller holding
 * a `File` would have to remember to upload it before submitting the step, and forgetting is
 * silent: the step succeeds and the resume is simply absent.
 *
 * **Failure stays on screen.** An upload that fails leaves the zone in an error state with
 * the server's own message and the file still named, rather than resetting to empty — the
 * user who just picked a 40 MB PDF needs to be told it was too large, not shown a fresh
 * prompt that implies nothing happened.
 */

import { motion, useReducedMotion } from 'framer-motion';
import { FileUp, Loader2, Paperclip, X } from 'lucide-react';
import { useCallback, useId, useRef, useState } from 'react';

import { ApiError } from '@/lib/api/client';
import { uploadFile } from '@/lib/api/endpoints';
import type { DocumentKind, FileRead } from '@/lib/api/types';
import { motionSafe, T, V } from '@/lib/motion';
import { cn } from '@/lib/utils';

import { Button } from './button';

/** Bytes in a kilobyte. Decimal, matching what every file manager shows. */
const BYTES_PER_KB = 1000;

/** Unit ladder for {@link formatBytes}. */
const SIZE_UNITS = ['B', 'kB', 'MB', 'GB'] as const;

/** Render a byte count the way a file manager would. */
function formatBytes(bytes: number): string {
  let value = bytes;
  let unit = 0;
  while (value >= BYTES_PER_KB && unit < SIZE_UNITS.length - 1) {
    value /= BYTES_PER_KB;
    unit += 1;
  }
  return `${unit === 0 ? value : value.toFixed(1)} ${SIZE_UNITS[unit]}`;
}

/** Props for {@link FileDropzone}. */
export interface FileDropzoneProps {
  /** What the file is. Drives retention and which screen lists it later. */
  kind?: DocumentKind;
  /** `accept` for the underlying input, e.g. `'.pdf,.docx'`. A hint, never a guarantee. */
  accept?: string;
  /** The already-uploaded file, if any. Pass `null` for an empty zone. */
  value?: FileRead | null;
  /** Called with the catalogue entry once the bytes are stored, or `null` when cleared. */
  onChange: (file: FileRead | null) => void;
  /** One line under the prompt. Say what you want, not how the control works. */
  hint?: string;
  disabled?: boolean;
  className?: string;
  /** Accessible name, when the surrounding `Field` does not already supply one. */
  'aria-label'?: string;
}

/**
 * Drag-or-browse upload that yields a catalogue id.
 *
 * @param props - See {@link FileDropzoneProps}.
 */
export function FileDropzone({
  kind = 'other',
  accept,
  value = null,
  onChange,
  hint,
  disabled = false,
  className,
  'aria-label': ariaLabel,
}: FileDropzoneProps) {
  const inputId = useId();
  const inputRef = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);
  const reduce = useReducedMotion();
  const variants = motionSafe(V.popIn, reduce);

  const send = useCallback(
    async (file: File) => {
      setBusy(true);
      setError(null);
      try {
        onChange(await uploadFile(file, kind));
      } catch (cause) {
        setError(
          cause instanceof ApiError
            ? (cause.detail ?? cause.message)
            : 'The file could not be uploaded.',
        );
      } finally {
        setBusy(false);
        // Clearing the input matters: without it, picking the same file again after an
        // error fires no `change` event at all, and the retry looks like a dead control.
        if (inputRef.current !== null) inputRef.current.value = '';
      }
    },
    [kind, onChange],
  );

  if (value !== null) {
    return (
      <motion.div
        className={cn(
          'flex items-center gap-3 rounded-lg border border-default bg-inset px-3 py-2.5',
          className,
        )}
        initial={variants.initial}
        animate={variants.animate}
        transition={T.pop}
      >
        <Paperclip aria-hidden="true" className="size-4 shrink-0 text-muted" />
        <span className="min-w-0 flex-1">
          <span className="block truncate text-sm text-primary">{value.filename}</span>
          <span className="block text-mini text-muted">{formatBytes(value.size_bytes)}</span>
        </span>
        <Button
          variant="ghost"
          size="sm"
          icon
          aria-label={`Remove ${value.filename}`}
          disabled={disabled}
          onClick={() => {
            setError(null);
            onChange(null);
          }}
        >
          <X aria-hidden="true" />
        </Button>
      </motion.div>
    );
  }

  return (
    <div className={cn('flex flex-col gap-1.5', className)}>
      <div
        className={cn(
          'relative flex flex-col items-center justify-center gap-2 rounded-lg border border-dashed px-4 py-7 text-center transition-colors',
          dragging ? 'border-accent-border bg-elevated' : 'border-strong bg-inset',
          error !== null && 'border-st-danger',
          disabled && 'opacity-60',
        )}
      >
        {busy ? (
          <Loader2 aria-hidden="true" className="size-5 animate-spin text-muted" />
        ) : (
          <FileUp aria-hidden="true" className="size-5 text-muted" />
        )}
        <span className="text-sm text-secondary">
          {busy ? (
            'Uploading…'
          ) : (
            <>
              <span className="text-primary underline underline-offset-2">Choose a file</span> or
              drag it here
            </>
          )}
        </span>
        {hint !== undefined && !busy && <span className="text-mini text-muted">{hint}</span>}

        {/*
          The input covers the zone rather than hiding behind a scripted click. Opacity zero
          rather than `hidden`, because a hidden input is not focusable and this control has
          to be reachable by keyboard like any other field on the step.
        */}
        <input
          ref={inputRef}
          id={inputId}
          type="file"
          className="absolute inset-0 cursor-pointer opacity-0 disabled:cursor-not-allowed"
          {...(accept === undefined ? {} : { accept })}
          {...(ariaLabel === undefined ? {} : { 'aria-label': ariaLabel })}
          disabled={disabled || busy}
          onDragEnter={() => {
            setDragging(true);
          }}
          onDragLeave={() => {
            setDragging(false);
          }}
          onDrop={() => {
            setDragging(false);
          }}
          onChange={(event) => {
            const picked = event.target.files?.[0];
            if (picked !== undefined) void send(picked);
          }}
        />
      </div>
      {error !== null && (
        <p role="alert" className="text-mini text-st-danger">
          {error}
        </p>
      )}
    </div>
  );
}
