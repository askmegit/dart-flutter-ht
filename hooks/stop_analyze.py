#!/usr/bin/env python3
"""End-of-turn Dart analysis gate for Claude Code sessions on this plugin.

Three hook events, all read from stdin as the Claude Code hook JSON
payload and keyed off ``hook_event_name``:

PostToolUse (Edit, Write, MultiEdit)
    When ``tool_input.file_path`` ends in ``.dart`` and lives inside a
    Dart project (nearest ancestor with a ``pubspec.yaml``), append a
    ``{"path": ..., "agent_id": ...}`` record to a per-session state file
    so the eventual Stop/SubagentStop check knows which files changed
    this turn. Fast, stdlib-only, no output -- never blocks the edit.
    Also opportunistically prunes state left behind by old sessions (see
    "State hygiene" below).

Stop / SubagentStop
    Runs every time the model tries to end its turn -- including on the
    automatic re-check Claude Code performs after this hook previously
    blocked (``stop_hook_active``); unlike a plain "run once and skip
    the rest" gate, this one has to actually look again, or a genuine
    fix (or a new regression) on the retry would never be seen.

    Scope: for SubagentStop, the ``.dart`` files recorded under that
    subagent's own ``agent_id``. For Stop (the main thread), the files
    recorded with no ``agent_id`` at all, plus the files of any subagent
    that has already *finished* its own Stop-checking lifecycle (see
    "Background subagents" below) -- a still-running subagent's edits
    are never analyzed or cleared by the main thread's own Stop.

    In scope, files are grouped by nearest package root and
    ``dart analyze --format=machine`` runs once per root (the whole
    package, so a broken caller in a different file is still caught),
    skipping a root whose dependencies were never installed (see
    "Pub workspaces" below) or whose own diagnostics we don't trust (exit
    code 64, or an unexpected exit with nothing parseable -- see
    "Analyze exit codes" below). Only ``ERROR``-severity diagnostics
    count, and only ones actually owned by the analyzed root itself --
    `dart analyze` can still walk into and report on a nested package
    (its own ``pubspec.yaml``, e.g. ``example/``, dependencies usually
    never installed there); those are dropped regardless of severity, or
    they'd spuriously block a turn on an unrelated package's problems.

    Each surviving error is reduced to a fingerprint
    (``relpath|code|message`` -- see "Fingerprints, not line numbers"
    below) and compared against a persisted per-scope "already reported"
    set B:

    * Not ``stop_hook_active`` (first check this turn): current errors
      E empty -> release the scope (clear its tracked edits and B) and
      let the turn end silently, UNLESS some root's `dart analyze`
      result was itself untrustworthy this round (see "Analyze exit
      codes" below) -- in that case E being empty doesn't mean "clean",
      it means "incomplete", so the scope is left as-is rather than
      released. E non-empty -> block, listing every error, and persist
      B = E (regardless of any untrustworthy root elsewhere in scope;
      real errors found elsewhere still block).
    * ``stop_hook_active`` (an automatic re-check after a previous
      block): NEW = E - B. NEW non-empty -> block, listing only the
      genuinely new fingerprints, and persist B = B | E. NEW empty ->
      release the scope UNCONDITIONALLY, even if some root's result was
      untrustworthy this round -- unlike the first-check case above,
      every fingerprint still in E was already in B (already shown
      once), so there's nothing this round could have told the model
      that it wasn't already told; refusing to release here would let a
      single flaky root re-block the *same, already-reported* error
      forever. This hook does not re-nag about a fingerprint a scope has
      already reported. Claude Code's own 8-consecutive-continuation cap
      (``CLAUDE_CODE_STOP_HOOK_BLOCK_CAP``) is the backstop against an
      infinite loop regardless -- if this hook somehow kept blocking,
      the 9th attempt ends the turn whether or not we agree, which can
      leave a scope's state file non-empty going into the next turn; the
      next Stop for that scope still starts from a fresh, un-diffed E
      either way (see the code).

    An analyze timeout releases the scope (clears its state) rather than
    leaving it dangling -- see "Timeouts" below.

Fingerprints, not line numbers
    A fingerprint is ``relpath|code|message``, deliberately omitting the
    line number. An edit anywhere earlier in the same file shifts every
    later line number, which would make an untouched pre-existing error
    look "new" on the ``stop_hook_active`` re-check (NEW = E - B) purely
    because it moved, forcing a repeat block over something the model
    neither caused nor could act on beyond what the first reason text
    already told it (at the old, now-stale line). ``code + message`` is
    specific enough in practice to treat two runs' reports of "the same
    complaint" in the same file as identical even if the line drifted.
    The block reason itself still shows the current ``line:col`` for
    each listed error -- only the de-duplication key omits it.

Background subagents
    A subagent's own SubagentStop passing (or timing out) does NOT
    delete its recorded edits -- they stay, still tagged with its
    ``agent_id``, and a finished-marker is appended for that
    ``agent_id``. A later main-thread Stop's scope is every entry with
    no ``agent_id`` plus every entry whose ``agent_id`` has a finished
    marker; a subagent that hasn't reached its own SubagentStop yet is
    invisible to the main thread's Stop, both for analysis and for
    clearing. When the main thread's own Stop eventually releases its
    scope, those absorbed entries (and their finished markers) are
    cleared along with everything else it checked.

    If a subagent's own SubagentStop declines to re-block an error it
    already reported once (NEW empty on its own ``stop_hook_active``
    retry -- e.g. it judged the error pre-existing), that fingerprint is
    only "forgiven" for the subagent's own scope; its finished marker
    still lets the main thread absorb the underlying edit later, and the
    main thread's own Stop runs its own independent first check against
    it (its own B starts empty), so the same error can legitimately
    surface once more there. This is intended, not a bug: the subagent
    declining to keep nagging about something it already flagged once is
    not the same as the main thread agreeing it's fine -- the main
    thread gets its own chance to react.

    A subagent's entries are only ever removed by (a) a later main Stop
    absorbing them once a finished marker exists, or (b) the 7-day
    prune. If a subagent never reaches its own SubagentStop with a clean
    result -- it keeps getting stuck at the 8-consecutive-continuation
    cap, or every check on it times out or hits an untrustworthy analyze
    result (see "Analyze exit codes") -- no finished marker is ever
    written, its entries are never absorbed by the main thread, and they
    simply sit in the session's state file until the 7-day prune sweeps
    them away. This is a deliberate simplicity tradeoff over building a
    second, subagent-specific expiry path.

    Known limitation: if Claude Code resumes a subagent under the same
    ``agent_id`` after it was already marked finished, a fresh batch of
    edits it makes before its next SubagentStop could be absorbed early
    by an in-between main Stop; this hook has no SubagentStart hook to
    detect a resume and retract the marker.

Pub workspaces
    A pub workspace resolves dependencies once at the workspace root, so
    a member package (its own ``pubspec.yaml`` with
    ``resolution: workspace``) never gets its own
    ``.dart_tool/package_config.json``. A root is only skipped for
    missing dependencies when it has neither its own
    ``.dart_tool/package_config.json`` nor -- for a workspace member --
    one at its nearest ancestor whose ``pubspec.yaml`` declares
    ``workspace:``. `dart analyze` itself still runs with the member's
    own directory as cwd; pub's own tooling walks up to the workspace's
    resolution from there.

Analyze exit codes
    Exit codes 0-3 are `dart analyze`'s normal range (no issues /
    hints-or-worse) and its machine-format output is trusted regardless
    of which of the four it is. Exit 64 (a usage error) is always
    treated as "skip this root" since the invocation itself was
    rejected, not the code. Any other exit code combined with literally
    nothing parseable in stdout is treated the same way, on the theory
    that an exit outside the documented range plus zero structured
    diagnostics means the tool failed rather than found a clean package.
    A failed or empty SDK resolution (before `dart analyze` even gets a
    chance to run) is treated the same way -- same "couldn't get a
    trustworthy answer from this root" bucket.

    Skipping a root never directly blocks or clears anything by itself
    -- it sets a scope-wide ``any_skipped`` flag that only changes what
    an *otherwise-empty* result means (see the Stop/SubagentStop bullet
    list above for the two different rules this implies for a first
    check versus a ``stop_hook_active`` retry). A root with real,
    parseable errors still blocks normally no matter what happened
    elsewhere in the same scope.

Timeouts
    All roots in a scope share ONE overall deadline
    (``DART_STOP_ANALYZE_TIMEOUT``, default 100s, comfortably under the
    Stop/SubagentStop hooks.json timeout of 120s) that also covers every
    SDK-resolve call, so one slow root can't silently starve the rest of
    the budget. Hitting the deadline anywhere aborts the whole check and
    releases the scope (clears its tracked edits and blocked-set) rather
    than leaving a check nobody could complete to be retried forever.
    Each subprocess (SDK resolve, `dart analyze`) starts in its own
    process group and, on an individual timeout, the whole group is
    killed -- not just the direct child -- so a `dart analyze`-spawned
    analysis-server descendant doesn't linger as an orphan.

State hygiene
    Every PostToolUse call opportunistically prunes the state directory
    (at most once per hour, throttled by a stamp file): any session's
    files (state jsonl, its lock, its per-scope blocked-set snapshots)
    older than 7 days are removed together, keyed off the state jsonl's
    own mtime so an idle-but-still-open lock file's unrelated mtime
    can't get an active session's lock deleted out from under it. A
    session's lock file is deliberately NEVER removed just because its
    state jsonl became empty -- unlinking a lock file while another
    process could be mid-``open()+flock()`` on that same path would
    split the lock (the unlinker's and a concurrent opener's `flock`
    calls would end up guarding two different inodes at the same path,
    no longer excluding each other); the 7-day prune is the only place
    a lock file is ever removed, and by then nothing should still be
    trying to open it.

Known limitation: only edits made through the Edit/Write/MultiEdit tools
are tracked. A ``.dart`` file rewritten via a Bash command (``sed``, a
Python one-liner, etc.) leaves no PostToolUse record here and will not
by itself trigger a re-check.

Contract: stdlib-only, never raises -- any exception anywhere is
swallowed and the process exits 0 with no output. ``Stop``/``SubagentStop``
convey their result via JSON on stdout (``decision``/``reason``), not via
a non-zero exit code, per the Claude Code hooks contract (JSON output is
read on every exit code). Set ``DART_STOP_ANALYZE=0`` to disable
entirely (all three hook paths).
"""
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

KILL_SWITCH_ENV = "DART_STOP_ANALYZE"
TIMEOUT_ENV = "DART_STOP_ANALYZE_TIMEOUT"
DEFAULT_TIMEOUT_SECONDS = 100
DART_LSP_RESOLVE_TIMEOUT_SECONDS = 15
MAX_REASON_LINES = 30
TRACKED_TOOLS = ("Edit", "Write", "MultiEdit")

ANALYZE_USAGE_ERROR_EXIT_CODE = 64
ANALYZE_NORMAL_EXIT_CODES = frozenset((0, 1, 2, 3))

PRUNE_STAMP_NAME = ".prune-stamp"
PRUNE_INTERVAL_SECONDS = 3600
PRUNE_MAX_AGE_SECONDS = 7 * 24 * 3600

_SAFE_ID_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
)


# ---------------------------------------------------------------------------
# State directory / per-session file
# ---------------------------------------------------------------------------

def _state_dir():
    base = os.environ.get("CLAUDE_PLUGIN_DATA")
    if base:
        return os.path.join(base, "stop-analyze")
    tmp = os.environ.get("TMPDIR") or tempfile.gettempdir() or "/tmp"
    return os.path.join(tmp, "dart-flutter-ht", "stop-analyze")


def _safe_session_id(session_id):
    if not isinstance(session_id, str) or not session_id:
        return None
    if not set(session_id) <= _SAFE_ID_CHARS:
        return None
    return session_id


def _session_file(session_id):
    return os.path.join(_state_dir(), session_id + ".jsonl")


class _FileLock(object):
    """Advisory exclusive lock guarding one session's state file.

    Best-effort: without ``fcntl`` (non-POSIX platforms) this degrades to
    no locking rather than raising. On POSIX, appends are additionally a
    single small ``write()`` call each, which is independently atomic --
    the lock mainly protects the read-modify-write clear/filter path.
    """

    def __init__(self, session_file):
        self._path = session_file + ".lock"
        self._fh = None

    def __enter__(self):
        try:
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            self._fh = open(self._path, "a+")
            if fcntl is not None:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
        except OSError:
            self._fh = None
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._fh is not None:
            try:
                if fcntl is not None:
                    fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            finally:
                self._fh.close()
        return False


def _read_session_lines(session_id):
    """Locked raw read of every JSON object line in this session's state
    file. ``session_id`` must already be sanitized by the caller."""
    session_file = _session_file(session_id)
    with _FileLock(session_file):
        try:
            with open(session_file, "r") as f:
                raw_lines = f.readlines()
        except OSError:
            return []
    objs = []
    for line in raw_lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            objs.append(obj)
    return objs


def _read_entries(session_id):
    return [o for o in _read_session_lines(session_id) if isinstance(o.get("path"), str)]


def _append_line(session_id, obj):
    session_file = _session_file(session_id)
    line = json.dumps(obj, ensure_ascii=True) + "\n"
    data = line.encode("utf-8")
    with _FileLock(session_file):
        try:
            os.makedirs(os.path.dirname(session_file), exist_ok=True)
            fd = os.open(session_file, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(fd, data)
            finally:
                os.close(fd)
        except OSError:
            pass


def _append_entry(session_id, path, agent_id):
    session_id = _safe_session_id(session_id)
    if not session_id:
        return
    _append_line(session_id, {"path": path, "agent_id": agent_id})


def _mark_agent_finished(session_id, agent_id):
    if not agent_id:
        return
    _append_line(session_id, {"finished_agent_id": agent_id})


def _rewrite_session_file(session_id, keep_fn):
    """Rewrite this session's state file keeping only lines for which
    ``keep_fn(parsed_obj)`` is True. Removes the file entirely when
    nothing survives -- the lock file is deliberately left in place (see
    the comment at its removal site) and is only ever cleaned up by
    ``_maybe_prune_state``'s 7-day sweep. ``session_id`` must already be
    sanitized."""
    session_file = _session_file(session_id)
    with _FileLock(session_file):
        try:
            with open(session_file, "r") as f:
                raw_lines = f.readlines()
        except OSError:
            return
        kept = []
        for raw in raw_lines:
            stripped = raw.strip()
            if not stripped:
                continue
            try:
                obj = json.loads(stripped)
            except ValueError:
                continue
            if not isinstance(obj, dict):
                continue
            if keep_fn(obj):
                kept.append(raw if raw.endswith("\n") else raw + "\n")
        try:
            if kept:
                tmp_path = "%s.tmp.%d" % (session_file, os.getpid())
                with open(tmp_path, "w") as f:
                    f.writelines(kept)
                os.replace(tmp_path, session_file)
            else:
                os.remove(session_file)
                # Deliberately do NOT also unlink the ".lock" file here:
                # a concurrent invocation could be blocked in _FileLock's
                # own open()+flock() on this exact path right now, and
                # unlinking out from under it would split the lock in
                # two -- our unlink, then flock() would silently create
                # and lock a *new* inode at the same path while the first
                # process is still holding (and will later unlock) the
                # *old* inode, so the two processes would never actually
                # exclude each other for the remainder of this window.
                # The lock file is harmless dead weight once its session
                # is empty; `_maybe_prune_state` removes it (grouped with
                # everything else sharing this session_id prefix) once
                # the session has been idle for 7 days.
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Per-scope blocked-set (the fingerprint set B already reported once)
# ---------------------------------------------------------------------------
#
# No file lock guards these reads/writes/removals: a given (session_id,
# scope_key) file has exactly one possible writer at a time -- the main
# thread's own Stop, or one specific subagent's SubagentStop -- since two
# Stop/SubagentStop hook invocations for the same scope can't be racing
# each other (Claude Code doesn't end the same turn's Stop twice
# concurrently, and two different subagents never share a scope_key).
# Unlike the session jsonl (which PostToolUse and Stop/SubagentStop can
# genuinely write concurrently), there's no reader/writer overlap here to
# protect against.

def _blocked_file(session_id, scope_key):
    kind, agent_id = scope_key
    if kind == "main":
        token = "main"
    else:
        token = "agent-" + hashlib.sha256(agent_id.encode("utf-8")).hexdigest()[:24]
    return os.path.join(_state_dir(), "%s.blocked.%s.json" % (session_id, token))


def _read_blocked_set(session_id, scope_key):
    path = _blocked_file(session_id, scope_key)
    try:
        with open(path, "r") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return set()
    fps = data.get("fingerprints") if isinstance(data, dict) else None
    if not isinstance(fps, list):
        return set()
    return set(x for x in fps if isinstance(x, str))


def _write_blocked_set(session_id, scope_key, fingerprints):
    path = _blocked_file(session_id, scope_key)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = "%s.tmp.%d" % (path, os.getpid())
        with open(tmp, "w") as f:
            json.dump({"fingerprints": sorted(fingerprints)}, f)
        os.replace(tmp, path)
    except OSError:
        pass


def _clear_blocked_set(session_id, scope_key):
    try:
        os.remove(_blocked_file(session_id, scope_key))
    except OSError:
        pass


def _release_subagent_scope(session_id, event_agent_id):
    """This subagent's own Stop-checking lifecycle is done (it passed, or
    we gave up on it after a timeout): drop its blocked-set and mark it
    finished. Its recorded edits are deliberately NOT removed here --
    they stay, still tagged with its agent_id, so a later main Stop can
    absorb and itself fully (re-)check whatever it left behind."""
    _clear_blocked_set(session_id, ("agent", event_agent_id))
    _mark_agent_finished(session_id, event_agent_id)


def _release_main_scope(session_id, absorbed_agent_ids):
    """The main thread's own Stop-checking lifecycle for this batch is
    done: drop every entry it just checked (its own agent_id=None edits,
    plus any finished subagents' edits it absorbed) along with their
    finished markers, and clear the main blocked-set."""
    scope_agent_ids = {None} | absorbed_agent_ids

    def keep(obj):
        if "path" in obj:
            return obj.get("agent_id") not in scope_agent_ids
        fid = obj.get("finished_agent_id")
        return not (isinstance(fid, str) and fid in absorbed_agent_ids)

    _rewrite_session_file(session_id, keep)
    _clear_blocked_set(session_id, ("main", None))


# ---------------------------------------------------------------------------
# State hygiene: opportunistic pruning of old sessions
# ---------------------------------------------------------------------------

def _maybe_prune_state():
    """Best-effort cleanup of state left behind by old sessions: at most
    once per hour (throttled by a stamp file), delete any session's
    files (state jsonl, lock, per-scope blocked-set snapshots) whose
    state jsonl is older than 7 days. Never raises; a failure here must
    not prevent the PostToolUse recording this call is piggybacking on.
    """
    try:
        state_dir = _state_dir()
        stamp_path = os.path.join(state_dir, PRUNE_STAMP_NAME)
        now = time.time()
        try:
            with open(stamp_path, "r") as f:
                last = float(f.read().strip())
        except (OSError, ValueError):
            last = 0.0
        if now - last < PRUNE_INTERVAL_SECONDS:
            return
        try:
            os.makedirs(state_dir, exist_ok=True)
            tmp = "%s.tmp.%d" % (stamp_path, os.getpid())
            with open(tmp, "w") as f:
                f.write(repr(now))
            os.replace(tmp, stamp_path)
        except OSError:
            pass

        cutoff = now - PRUNE_MAX_AGE_SECONDS
        try:
            names = os.listdir(state_dir)
        except OSError:
            return

        # Group every file by its session_id prefix (session_id itself
        # never contains "." -- see _SAFE_ID_CHARS -- so splitting on the
        # first "." cleanly recovers it from "<id>.jsonl",
        # "<id>.jsonl.lock", and "<id>.blocked.<scope>.json" alike) so a
        # lock file's own mtime (which doesn't change just because the
        # session is actively using it -- flock doesn't touch mtime)
        # can't get an active session's lock deleted out from under it;
        # age is judged from the live state jsonl instead.
        groups = {}
        for name in names:
            if name == PRUNE_STAMP_NAME:
                continue
            key = name.split(".", 1)[0]
            groups.setdefault(key, []).append(name)

        for key, names_in_group in groups.items():
            jsonl_name = key + ".jsonl"
            ref_mtime = None
            if jsonl_name in names_in_group:
                try:
                    ref_mtime = os.stat(os.path.join(state_dir, jsonl_name)).st_mtime
                except OSError:
                    ref_mtime = None
            if ref_mtime is None:
                # Orphan group (no live .jsonl, e.g. a blocked-set file
                # left behind by a partially-failed clear): fall back to
                # the newest mtime among what's left.
                mtimes = []
                for n in names_in_group:
                    try:
                        mtimes.append(os.stat(os.path.join(state_dir, n)).st_mtime)
                    except OSError:
                        pass
                ref_mtime = max(mtimes) if mtimes else None
            if ref_mtime is None or ref_mtime >= cutoff:
                continue
            for n in names_in_group:
                try:
                    os.remove(os.path.join(state_dir, n))
                except OSError:
                    pass
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Dart-project / package-root detection
# ---------------------------------------------------------------------------

def _find_pubspec_root(path):
    if not path:
        return None
    try:
        cur = os.path.abspath(path)
    except (TypeError, ValueError):
        return None
    if os.path.isfile(cur):
        cur = os.path.dirname(cur)
    hops = 0
    while cur and hops < 64:
        try:
            if os.path.isfile(os.path.join(cur, "pubspec.yaml")):
                return cur
        except OSError:
            return None
        parent = os.path.dirname(cur)
        if parent == cur:
            return None
        cur = parent
        hops += 1
    return None


def _group_by_root(paths):
    order = []
    seen = set()
    for p in paths:
        root = _find_pubspec_root(p)
        if not root or root in seen:
            continue
        seen.add(root)
        order.append(root)
    return order


# ---------------------------------------------------------------------------
# Pub workspaces
# ---------------------------------------------------------------------------

_WORKSPACE_MEMBER_RE = re.compile(r"(?m)^\s*resolution\s*:\s*workspace\s*$")
_WORKSPACE_ROOT_RE = re.compile(r"(?m)^\s*workspace\s*:")


def _read_pubspec_text(root):
    try:
        with open(os.path.join(root, "pubspec.yaml"), "r", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def _is_workspace_member(root):
    text = _read_pubspec_text(root)
    return bool(text) and bool(_WORKSPACE_MEMBER_RE.search(text))


def _find_workspace_root(root):
    cur = os.path.dirname(root)
    hops = 0
    while cur and hops < 64:
        text = _read_pubspec_text(cur)
        if text is not None and _WORKSPACE_ROOT_RE.search(text):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return None
        cur = parent
        hops += 1
    return None


def _has_installed_deps(root):
    """True if ``root`` can be analyzed -- either it has its own
    ``.dart_tool/package_config.json``, or it's a pub workspace member
    (``resolution: workspace`` in its own pubspec.yaml) whose nearest
    workspace-root ancestor (a pubspec.yaml declaring ``workspace:``) has
    one instead. See "Pub workspaces" in the module docstring."""
    if os.path.isfile(os.path.join(root, ".dart_tool", "package_config.json")):
        return True
    if not _is_workspace_member(root):
        return False
    workspace_root = _find_workspace_root(root)
    if not workspace_root:
        return False
    return os.path.isfile(os.path.join(workspace_root, ".dart_tool", "package_config.json"))


# ---------------------------------------------------------------------------
# Dart SDK resolution / analyze invocation
# ---------------------------------------------------------------------------

def _plugin_root():
    env_root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if env_root:
        return env_root
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _run_with_timeout(cmd, cwd, timeout, env=None):
    """Runs ``cmd``, returning ("ok", returncode, stdout_bytes) /
    ("timeout", None, b"") / ("error", None, b""). The child starts in
    its own process group (``start_new_session=True``) and, on timeout,
    the whole group is killed -- not just the direct child -- so a
    ``dart analyze``-spawned analysis-server descendant doesn't linger
    as an orphan.
    """
    if timeout <= 0:
        return "timeout", None, b""
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError:
        return "error", None, b""
    try:
        stdout, _stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            proc.communicate(timeout=5)
        except Exception:
            pass
        return "timeout", None, b""
    except OSError:
        return "error", None, b""
    return "ok", proc.returncode, stdout


def _resolve_dart(root, timeout):
    """Returns ("ok", dart_path) / ("timeout", None) / ("skip", None)."""
    dart_lsp = os.path.join(_plugin_root(), "bin", "dart-lsp")
    env = dict(os.environ)
    env["DART_LSP_PRINT"] = "1"
    status, returncode, stdout = _run_with_timeout([dart_lsp], root, timeout, env=env)
    if status == "timeout":
        return "timeout", None
    if status != "ok" or returncode != 0:
        return "skip", None
    out = stdout.decode("utf-8", errors="replace").strip()
    return ("ok", out) if out else ("skip", None)


def _timeout_seconds():
    raw = os.environ.get(TIMEOUT_ENV)
    if raw is None:
        return DEFAULT_TIMEOUT_SECONDS
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_TIMEOUT_SECONDS
    return value if value > 0 else DEFAULT_TIMEOUT_SECONDS


def _unescape_message(msg):
    return msg.replace("\\n", " ").replace("\\|", "|")


def _parse_machine_output(text):
    results = []
    for raw_line in text.splitlines():
        line = raw_line.strip("\r")
        if not line:
            continue
        parts = line.split("|", 7)
        if len(parts) != 8:
            continue
        severity, typ, code, file_path, line_no, col_no, length, message = parts
        try:
            line_no = int(line_no)
            col_no = int(col_no)
            length = int(length)
        except ValueError:
            continue
        results.append({
            "severity": severity,
            "type": typ,
            "code": code,
            "file": file_path,
            "line": line_no,
            "col": col_no,
            "len": length,
            "message": _unescape_message(message),
        })
    return results


def _run_analyze(dart_bin, root, timeout):
    """Returns ("ok", errors) / ("timeout", []) / ("skip", []). See
    "Analyze exit codes" in the module docstring for the skip rules."""
    status, returncode, stdout = _run_with_timeout(
        [dart_bin, "analyze", "--format=machine", "."], root, timeout,
    )
    if status == "timeout":
        return "timeout", []
    if status != "ok":
        return "skip", []
    out = stdout.decode("utf-8", errors="replace")
    parsed = _parse_machine_output(out)
    if returncode == ANALYZE_USAGE_ERROR_EXIT_CODE:
        return "skip", []
    if returncode not in ANALYZE_NORMAL_EXIT_CODES and not parsed:
        return "skip", []
    errors = [e for e in parsed if e["severity"] == "ERROR"]
    return "ok", errors


def _owned_by_root(file_path, root_real):
    """True if ``file_path``'s own nearest ``pubspec.yaml`` is ``root_real``
    (already realpath'd) rather than some nested package below it (e.g. an
    ``example/`` or ``packages/*`` subdirectory with its own pubspec.yaml).

    This is the authoritative filter: `dart analyze` run at a package root
    can still surface diagnostics for a nested package it happens to walk
    into (typically all-false-error noise, since that nested package's own
    deps were never installed), and those must never count toward blocking
    a turn on an unrelated package's problems.
    """
    try:
        real_file = os.path.realpath(file_path)
    except (OSError, TypeError, ValueError):
        return False
    owner = _find_pubspec_root(real_file)
    if not owner:
        return False
    try:
        return os.path.realpath(owner) == root_real
    except (OSError, TypeError, ValueError):
        return owner == root_real


def _fingerprint(err, root_real):
    try:
        rel = os.path.relpath(os.path.realpath(err["file"]), root_real)
    except (OSError, TypeError, ValueError):
        rel = err["file"]
    # relpath|code|message, deliberately NOT line -- see "Fingerprints,
    # not line numbers" in the module docstring.
    return "%s|%s|%s" % (rel, err["code"], err["message"])


def _run_analysis_for_roots(roots, overall_timeout_seconds):
    """Returns ("ok", all_errors, any_skipped) or ("timeout", [], False).
    Each error dict in ``all_errors`` additionally carries a ``"fp"``
    fingerprint key. ``any_skipped`` is True if at least one root's
    `dart analyze` invocation itself was untrustworthy (exit 64, or a
    nonzero exit with nothing parseable -- see "Analyze exit codes" in
    the module docstring) -- as opposed to a root skipped because its
    deps were never installed, which is a confident "not part of the
    picture" and does not set this flag. The caller uses it to avoid
    treating "found zero errors, but only because one root's result was
    unusable" as equivalent to a genuinely clean scope.

    All roots share ONE overall deadline (including every SDK-resolve and
    `dart analyze` subprocess call) -- see "Timeouts" in the module
    docstring.
    """
    deadline = time.monotonic() + overall_timeout_seconds
    all_errors = []
    any_skipped = False
    for root in roots:
        if deadline - time.monotonic() <= 0:
            return "timeout", [], False
        if not _has_installed_deps(root):
            continue

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return "timeout", [], False
        status, dart_bin = _resolve_dart(root, min(remaining, DART_LSP_RESOLVE_TIMEOUT_SECONDS))
        if status == "timeout":
            return "timeout", [], False
        if status != "ok" or not dart_bin:
            # SDK resolution itself failed or produced nothing usable
            # (e.g. bin/dart-lsp errored or printed nothing) -- just as
            # untrustworthy as an unusable `dart analyze` exit, so this
            # counts the same way toward any_skipped.
            any_skipped = True
            continue

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return "timeout", [], False
        status, errors = _run_analyze(dart_bin, root, remaining)
        if status == "timeout":
            return "timeout", [], False
        if status == "skip":
            any_skipped = True
            continue

        try:
            root_real = os.path.realpath(root)
        except (OSError, TypeError, ValueError):
            root_real = root
        for e in errors:
            if _owned_by_root(e["file"], root_real):
                e["fp"] = _fingerprint(e, root_real)
                all_errors.append(e)
    return "ok", all_errors, any_skipped


# ---------------------------------------------------------------------------
# Reason message building
# ---------------------------------------------------------------------------

def _format_error_line(err, base_dir):
    # `dart analyze` reports paths resolved through the real filesystem
    # path (e.g. macOS's /tmp -> /private/tmp), so both sides must be
    # realpath'd or a symlinked ancestor anywhere in cwd/the project turns
    # every relative path into a spurious "../../..." climb back down
    # through the symlink target.
    file_path = err["file"]
    rel = file_path
    if base_dir:
        try:
            rel = os.path.relpath(os.path.realpath(file_path), os.path.realpath(base_dir))
        except (OSError, TypeError, ValueError):
            rel = file_path
    return "%s:%d:%d %s %s" % (rel, err["line"], err["col"], err["code"], err["message"])


def _build_reason(errors, edited_real_paths, base_dir):
    edited_errors = []
    other_errors = []
    for err in errors:
        try:
            # See _format_error_line: realpath, not abspath, so a
            # symlinked ancestor (e.g. macOS /tmp) doesn't make an
            # edited file's own error fail to match itself.
            is_edited = os.path.realpath(err["file"]) in edited_real_paths
        except (OSError, TypeError, ValueError):
            is_edited = False
        (edited_errors if is_edited else other_errors).append(err)

    def _sort_key(e):
        return (e["file"], e["line"], e["col"])

    edited_errors.sort(key=_sort_key)
    other_errors.sort(key=_sort_key)
    ordered = edited_errors + other_errors

    total = len(ordered)
    if total > MAX_REASON_LINES:
        shown = ordered[: MAX_REASON_LINES - 1]
    else:
        shown = ordered

    lines = [_format_error_line(err, base_dir) for err in shown]
    if total > len(shown):
        lines.append("+%d more" % (total - len(shown)))

    lines.append("")
    lines.append(
        "Fix these before finishing. If an error pre-existed and is "
        "unrelated to your change, say so and stop again."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Hook event handlers
# ---------------------------------------------------------------------------

def _emit(payload):
    sys.stdout.write(json.dumps(payload))


def handle_post_tool_use(data):
    _maybe_prune_state()
    if data.get("tool_name") not in TRACKED_TOOLS:
        return
    tool_input = data.get("tool_input")
    if not isinstance(tool_input, dict):
        return
    file_path = tool_input.get("file_path")
    if not isinstance(file_path, str) or not file_path.endswith(".dart"):
        return
    if not _find_pubspec_root(file_path):
        return
    _append_entry(data.get("session_id"), file_path, data.get("agent_id"))


def handle_stop_like(data, is_subagent):
    session_id = _safe_session_id(data.get("session_id"))
    if not session_id:
        return

    event_agent_id = None
    if is_subagent:
        event_agent_id = data.get("agent_id")
        if not isinstance(event_agent_id, str) or not event_agent_id:
            # Not every SubagentStop comes from a subagent Claude spawned
            # -- Claude Code's own internal agents (prompt suggestions,
            # /btw side questions) fire it too, and per the hooks docs
            # their agent_id may be absent. With no agent_id to scope by,
            # there is no principled subset of this session's entries to
            # touch, so do nothing.
            return

    stop_hook_active = bool(data.get("stop_hook_active"))

    raw_objs = _read_session_lines(session_id)
    entries = [o for o in raw_objs if isinstance(o.get("path"), str)]
    finished_agents = set(
        o["finished_agent_id"] for o in raw_objs
        if isinstance(o.get("finished_agent_id"), str) and o["finished_agent_id"]
    )

    if is_subagent:
        in_scope = [e for e in entries if e.get("agent_id") == event_agent_id]
    else:
        scope_agent_ids = {None} | finished_agents
        in_scope = [e for e in entries if e.get("agent_id") in scope_agent_ids]

    paths = []
    seen = set()
    for e in in_scope:
        p = e.get("path")
        if isinstance(p, str) and p not in seen:
            seen.add(p)
            paths.append(p)
    if not paths:
        return

    roots = _group_by_root(paths)
    if not roots:
        return

    def release():
        if is_subagent:
            _release_subagent_scope(session_id, event_agent_id)
        else:
            _release_main_scope(session_id, finished_agents)

    status, errors, any_skipped = _run_analysis_for_roots(roots, _timeout_seconds())
    if status == "timeout":
        release()
        return

    scope_key = ("agent", event_agent_id) if is_subagent else ("main", None)
    current_fps = set(e["fp"] for e in errors)

    if stop_hook_active:
        blocked = _read_blocked_set(session_id, scope_key)
        report_fps = current_fps - blocked
        if not report_fps:
            # Nothing NEW to report on this re-check -- release
            # unconditionally, even if any_skipped. Unlike the first
            # check below, staying silent here doesn't cost us any
            # signal: every fingerprint still in current_fps was already
            # shown once (it's in `blocked`), so the model has already
            # had its one chance to react to it. Refusing to release
            # just because some other root's analyze was untrustworthy
            # this round would leave a genuinely pre-existing, already
            # reported error re-blocking every later turn that happens
            # to touch the same file, forever -- the main thread still
            # gets its own independent check of the same files later
            # (see "Background subagents" for the subagent-scope case of
            # this), so nothing is silently lost, only deferred.
            release()
            return
        next_blocked = blocked | current_fps
    else:
        if not current_fps:
            # First check this turn: unlike above, there is no prior
            # blocked-set to fall back on, so if a root's result is
            # untrustworthy we genuinely don't know whether this scope
            # is clean -- don't release, leave it for the next check.
            if not any_skipped:
                release()
            return
        report_fps = current_fps
        next_blocked = current_fps

    _write_blocked_set(session_id, scope_key, next_blocked)
    report_errors = [e for e in errors if e["fp"] in report_fps]

    edited_real = set()
    for p in paths:
        try:
            edited_real.add(os.path.realpath(p))
        except (OSError, TypeError, ValueError):
            pass
    cwd = data.get("cwd")
    base_dir = cwd if isinstance(cwd, str) else roots[0]
    reason = _build_reason(report_errors, edited_real, base_dir)
    _emit({"decision": "block", "reason": reason})


def main():
    if os.environ.get(KILL_SWITCH_ENV) == "0":
        return
    raw = sys.stdin.read()
    if not raw or not raw.strip():
        return
    data = json.loads(raw)
    if not isinstance(data, dict):
        return
    event = data.get("hook_event_name")
    if event == "PostToolUse":
        handle_post_tool_use(data)
    elif event == "Stop":
        handle_stop_like(data, is_subagent=False)
    elif event == "SubagentStop":
        handle_stop_like(data, is_subagent=True)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
