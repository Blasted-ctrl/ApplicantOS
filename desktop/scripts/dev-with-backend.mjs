/**
 * Development launcher: one command that guarantees a backend and a frontend, and that neither
 * outlives the other.
 *
 * `npm run dev` is what `tauri dev` invokes as its `beforeDevCommand`, so this script sits
 * directly under the Rust shell in development. It does four things, and each exists because of
 * a specific way the naive version fails.
 *
 * **Health-check before spawning — but only against a backend of ours.** A second uvicorn on a
 * second port opens the same SQLite file as the first, and on Windows the second one loses.
 * `docs/CONTRACTS.md` §18 names an orphaned uvicorn holding that file as this architecture's
 * most common failure, and re-running `npm run dev` in a second terminal is the easiest way to
 * create one. So a run adopts the backend a previous run *recorded* rather than whatever
 * happens to answer on port 8000 — see {@link adoptablePort}. Adopting a stranger is how you
 * end up editing one codebase and running another.
 *
 * **The handoff file.** The Rust shell has no way to learn a port chosen by a Node script in
 * another process, and hardcoding one would defeat the runtime port selection in
 * `src-tauri/src/sidecar.rs`. The port is written to `desktop/.dev-backend.json`, which a debug
 * build reads and attaches to. It is deleted on exit, so a stale file cannot point the next run
 * at a backend that is gone — and even if it survives a hard kill, the shell health-checks the
 * port before trusting it.
 *
 * **Kill the frontend if the backend dies.** A Vite server that keeps serving after the backend
 * has crashed produces an app that renders perfectly and answers nothing, which reads as a
 * frontend bug and is not one. Failing loudly and together is faster to diagnose.
 *
 * **Print the command to reproduce.** When the backend fails to start, the interesting output is
 * a Python traceback that this script has already captured — so it is printed, followed by the
 * exact command to run it in the foreground.
 *
 * Windows is a first-class target (`CLAUDE.md`), so the interpreter is looked for at
 * `Scripts/python.exe` before `bin/python`, and every child is started with `shell: false`.
 */

import { spawn, spawnSync } from 'node:child_process';
import { existsSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { createServer } from 'node:net';
import { dirname, join, resolve } from 'node:path';
import process from 'node:process';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const DESKTOP_DIR = resolve(HERE, '..');
const REPO_ROOT = resolve(DESKTOP_DIR, '..');
const HANDOFF_FILE = join(DESKTOP_DIR, '.dev-backend.json');

/** Loopback host. The dev backend is never bound to anything routable. */
const HOST = '127.0.0.1';

/** Port used when one is already free; matches `Settings.api_port`. */
const PREFERRED_PORT = 8000;

/** Health polls before giving up. 60 x 500ms = 30 seconds. */
const HEALTH_ATTEMPTS = 60;

/** Gap between health polls, in milliseconds. */
const HEALTH_INTERVAL_MS = 500;

/** Timeout for a single health request, in milliseconds. */
const HEALTH_TIMEOUT_MS = 1500;

/** Timeout for the one probe that decides whether a backend is already running. */
const ADOPT_TIMEOUT_MS = 700;

/** Backend output lines kept for the failure report. */
const LOG_TAIL_LINES = 60;

/**
 * Wrap text in an ANSI SGR code, or leave it alone when stdout is not a terminal — escape
 * sequences in a piped or CI log are noise, not colour.
 *
 * @param {number} code SGR parameter.
 * @returns {(text: string) => string}
 */
function sgr(code) {
  /** @param {string} text */
  return (text) => (process.stdout.isTTY ? `\u001b[${code}m${text}\u001b[0m` : text);
}

/** ANSI helpers. */
const colour = {
  dim: sgr(2),
  red: sgr(31),
  green: sgr(32),
  yellow: sgr(33),
};

/**
 * Print a prefixed line.
 *
 * @param {string} message
 */
function log(message) {
  console.log(`${colour.dim('[dev]')} ${message}`);
}

/**
 * Ask the OS for a free TCP port, preferring {@link PREFERRED_PORT}.
 *
 * @returns {Promise<number>} A port that was free a moment ago.
 */
function findPort() {
  return new Promise((resolvePort) => {
    /** @param {number} candidate */
    const attempt = (candidate) => {
      const server = createServer();
      server.once('error', () => {
        if (candidate === 0) {
          resolvePort(PREFERRED_PORT);
          return;
        }
        attempt(0);
      });
      server.listen(candidate, HOST, () => {
        const address = server.address();
        const port = typeof address === 'object' && address !== null ? address.port : candidate;
        server.close(() => resolvePort(port));
      });
    };
    attempt(PREFERRED_PORT);
  });
}

/**
 * Issue one `GET /health`.
 *
 * @param {number} port
 * @param {number} timeoutMs
 * @returns {Promise<boolean>} Whether a backend answered.
 */
async function isHealthy(port, timeoutMs) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(`http://${HOST}:${port}/health`, { signal: controller.signal });
    return response.ok;
  } catch {
    return false;
  } finally {
    clearTimeout(timer);
  }
}

/**
 * Locate the project's Python interpreter.
 *
 * Falling through to a bare `python` on PATH is deliberately not done: the system interpreter
 * almost certainly lacks this project's dependencies, and `ModuleNotFoundError: fastapi` is a
 * much worse error message than "no virtualenv found, here is how to make one".
 *
 * @returns {string | null} An absolute path, or null when nothing usable was found.
 */
function findPython() {
  const override = process.env['APPLICANTOS_PYTHON'];
  if (override && existsSync(override)) {
    return override;
  }
  const relative = process.platform === 'win32' ? ['Scripts', 'python.exe'] : ['bin', 'python'];
  for (const venv of ['.venv', 'venv']) {
    const candidate = join(REPO_ROOT, venv, ...relative);
    if (existsSync(candidate)) {
      return candidate;
    }
  }
  return null;
}

/**
 * Format the command a human should run to see the backend's own output.
 *
 * @param {string} python
 * @param {number} port
 * @returns {string}
 */
function reproduceCommand(python, port) {
  return `"${python}" -m uvicorn app.main:app --host ${HOST} --port ${port}`;
}

/** Child processes to tear down, newest first. */
/** @type {import('node:child_process').ChildProcess[]} */
const children = [];

/** Guards against the shutdown path running twice on SIGINT followed by an exit event. */
let shuttingDown = false;

/**
 * Stop every child and remove the handoff file.
 *
 * @param {number} code Exit status for this process.
 */
function shutdown(code) {
  if (shuttingDown) {
    return;
  }
  shuttingDown = true;

  rmSync(HANDOFF_FILE, { force: true });

  for (const child of children.reverse()) {
    if (child.exitCode === null && child.signalCode === null) {
      // On Windows `kill` does not reach grandchildren, and uvicorn's reloader has one — the
      // worker process that actually holds the SQLite file. `taskkill /T` takes the tree, and
      // it must be `spawnSync`: `process.exit` below would otherwise terminate this process
      // before an async child had a chance to start, leaving exactly the orphaned uvicorn
      // that `docs/CONTRACTS.md` §18 warns about.
      if (process.platform === 'win32' && child.pid !== undefined) {
        spawnSync('taskkill', ['/pid', String(child.pid), '/T', '/F'], {
          stdio: 'ignore',
          shell: false,
        });
      } else {
        child.kill('SIGTERM');
      }
    }
  }

  process.exit(code);
}

/**
 * Start the backend and wait for it to answer, or explain why it did not.
 *
 * @param {number} port
 * @returns {Promise<void>}
 */
async function startBackend(port) {
  const python = findPython();
  if (python === null) {
    console.error(
      colour.red('[dev] No Python interpreter found.') +
        `\n      Looked for ${join(REPO_ROOT, '.venv')} and ${join(REPO_ROOT, 'venv')}.` +
        '\n      Create one and install the project:' +
        `\n\n        python -m venv .venv` +
        `\n        ${process.platform === 'win32' ? '.venv\\Scripts\\pip' : '.venv/bin/pip'} install -e ".[sqlite]" uvicorn` +
        '\n\n      Or set APPLICANTOS_PYTHON to an interpreter that already has it.',
    );
    process.exit(1);
  }

  log(`starting backend on ${HOST}:${port} (${python})`);

  const backend = spawn(
    python,
    ['-m', 'uvicorn', 'app.main:app', '--host', HOST, '--port', String(port), '--reload'],
    {
      cwd: REPO_ROOT,
      shell: false,
      stdio: ['ignore', 'pipe', 'pipe'],
      env: {
        ...process.env,
        SQLITE_MODE: 'true',
        API_HOST: HOST,
        API_PORT: String(port),
        APPLICANTOS_MANAGED_BY: 'dev-launcher',
        PYTHONUNBUFFERED: '1',
      },
    },
  );
  children.push(backend);

  /** @type {string[]} */
  const tail = [];
  /** @param {Buffer} chunk */
  const capture = (chunk) => {
    const text = chunk.toString();
    process.stdout.write(text);
    for (const line of text.split(/\r?\n/)) {
      if (line.trim().length > 0) {
        tail.push(line);
        if (tail.length > LOG_TAIL_LINES) {
          tail.shift();
        }
      }
    }
  };
  backend.stdout?.on('data', capture);
  backend.stderr?.on('data', capture);

  let backendExited = false;
  backend.on('exit', (code, signal) => {
    backendExited = true;
    if (!shuttingDown) {
      console.error(
        colour.red(`\n[dev] The backend exited (code ${code}, signal ${signal}).`) +
          '\n      Stopping the frontend too — a UI with no API behind it looks like a UI bug.' +
          `\n\n      Reproduce it in the foreground:\n\n        cd "${REPO_ROOT}"\n        ${reproduceCommand(python, port)}\n`,
      );
      shutdown(1);
    }
  });

  for (let attempt = 0; attempt < HEALTH_ATTEMPTS; attempt += 1) {
    if (backendExited) {
      return;
    }
    if (await isHealthy(port, HEALTH_TIMEOUT_MS)) {
      log(colour.green(`backend ready on http://${HOST}:${port}`));
      return;
    }
    await new Promise((done) => setTimeout(done, HEALTH_INTERVAL_MS));
  }

  const seconds = (HEALTH_ATTEMPTS * HEALTH_INTERVAL_MS) / 1000;
  console.error(
    colour.red(`\n[dev] The backend did not answer /health within ${seconds}s.`) +
      (tail.length > 0 ? `\n\n${tail.join('\n')}\n` : '') +
      `\n      Run it in the foreground to see the full traceback:\n\n        cd "${REPO_ROOT}"\n        ${reproduceCommand(python, port)}\n`,
  );
  shutdown(1);
}

/**
 * Start Vite and mirror its exit status.
 *
 * @returns {void}
 */
function startFrontend() {
  log('starting vite');
  const vite = spawn(process.execPath, [join(DESKTOP_DIR, 'node_modules', 'vite', 'bin', 'vite.js')], {
    cwd: DESKTOP_DIR,
    shell: false,
    stdio: 'inherit',
  });
  children.push(vite);

  vite.on('exit', (code) => {
    if (!shuttingDown) {
      log('vite exited, stopping the backend');
      shutdown(code ?? 0);
    }
  });
}

/**
 * The port this launcher may adopt, or `null` when it must start its own backend.
 *
 * An explicit `APPLICANTOS_BACKEND_PORT` is a deliberate instruction and wins. Otherwise the
 * only adoptable backend is one a previous run recorded in the handoff file — see the note in
 * {@link main} for why a bare probe of {@link PREFERRED_PORT} is not good enough.
 *
 * @returns {number | null} The port to health-check, or `null`.
 */
function adoptablePort() {
  const configured = Number.parseInt(process.env['APPLICANTOS_BACKEND_PORT'] ?? '', 10);
  if (Number.isInteger(configured)) return configured;

  if (!existsSync(HANDOFF_FILE)) return null;
  try {
    const recorded = JSON.parse(readFileSync(HANDOFF_FILE, 'utf8'));
    return Number.isInteger(recorded?.port) ? recorded.port : null;
  } catch {
    return null; // a truncated or hand-edited file is not a record of anything
  }
}

async function main() {
  process.on('SIGINT', () => shutdown(0));
  process.on('SIGTERM', () => shutdown(0));

  // Adopt a backend only when we can point at the record of having started it.
  //
  // This used to probe PREFERRED_PORT unconditionally and adopt anything that answered
  // `/health`. That is fine right up until something *else* is on 8000 — most often an
  // orphaned uvicorn from a hard-killed session, running the code as it was that day. The
  // launcher would then hand the frontend a stale build over a stale database, and the
  // symptom is the worst kind: the app works, it is simply not the app you are editing.
  // `/health` cannot tell them apart either, and rightly does not report the data directory
  // it opened.
  //
  // So the rule is "adopt what I recorded, never a stranger". The handoff file is written on
  // start and removed on exit, which is exactly the record of "a dev backend of mine is
  // live"; an explicit APPLICANTOS_BACKEND_PORT still wins, because that is someone saying
  // so on purpose. Anything else gets a fresh backend on a free port, and `findPort` skips
  // the occupied one on its own.
  const adoptable = adoptablePort();

  let port;
  if (adoptable !== null && (await isHealthy(adoptable, ADOPT_TIMEOUT_MS))) {
    port = adoptable;
    log(colour.yellow(`reusing the backend already running on ${HOST}:${adoptable}`));
  } else {
    port = await findPort();
    await startBackend(port);
  }

  writeFileSync(HANDOFF_FILE, `${JSON.stringify({ port, host: HOST }, null, 2)}\n`, 'utf8');
  process.env['APPLICANTOS_BACKEND_PORT'] = String(port);
  process.env['VITE_APPLICANTOS_BACKEND_PORT'] = String(port);

  startFrontend();
}

main().catch((error) => {
  console.error(colour.red('[dev] launcher failed:'), error);
  shutdown(1);
});
