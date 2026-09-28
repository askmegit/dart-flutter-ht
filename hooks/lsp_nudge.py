#!/usr/bin/env python3
"""LSP-vs-grep steering nudges for Dart/Flutter Claude Code sessions.

Two hook events, both read from stdin as the Claude Code hook JSON payload
and both keyed off ``hook_event_name``:

SessionStart
    If the session's cwd is inside a Dart project (nearest ancestor with a
    ``pubspec.yaml``), emit a short comparative/anti-rationalization
    ``additionalContext`` blurb steering the model toward the native
    (deferred) ``LSP`` tool for symbol work, modeled on the evidence in
    ``.omc/research/agent-lsp-trigger-patterns.md`` (comparative framing +
    an explicit anti-rationalization list beat a bare imperative CLAUDE.md
    rule).

PreToolUse (Bash, Grep, Read)
    Emits a short ``additionalContext`` hint -- never a deny, never a
    permissionDecision, never a rewritten tool input -- when the tool call
    looks like exactly the shape
    ``.omc/research/lsp-missed-opportunities.md`` ("Mechanical
    detectability at PreToolUse time") found to be both detectable from
    tool input alone and high precision: a grep/rg search over Dart source
    whose pattern contains an identifier-like Dart symbol, or an
    unranged Read of a large ``.dart`` file. The same report's precision
    row also lists concrete false-positive shapes (grep filtering another
    command's own output, snake_case JSON-key alternatives, git-ref
    archaeology, single generic words) -- this module excludes all of
    them explicitly; see the corresponding helper functions below.

Contract (see also ht-agent-plugins task brief): stdlib-only, fast
(<100ms), and never raises -- any exception anywhere is swallowed and the
process exits 0 with no output. Never blocks a tool call, never sets
``permissionDecision``, never modifies tool input. Set
``DART_LSP_NUDGE=0`` to disable entirely (used by both hook events).
"""
import json
import os
import re
import sys

# ---------------------------------------------------------------------------
# Kill switch
# ---------------------------------------------------------------------------

KILL_SWITCH_ENV = "DART_LSP_NUDGE"

# ---------------------------------------------------------------------------
# Vocabulary / regexes shared by the Bash-command and native-Grep-tool paths
# ---------------------------------------------------------------------------

# Words that collide too often with unrelated searches (JSON keys, generic
# English) to trust as a Dart-symbol signal on their own. Carried over from
# .omc/research/lsp-missed-opportunities.py's validated GENERIC_WORDS set.
GENERIC_WORDS = frozenset({
    "text", "login", "token", "userid", "uid", "print", "log", "data",
    "index", "value", "name", "id", "type", "state", "item", "list",
    "get", "set", "test", "class", "source", "with", "map", "key",
})

# Framework lifecycle/override methods: grepping these hits every override in
# the codebase (virtual dispatch), so findReferences is noisy there too --
# don't nudge. Keys are lowercased last-segment identifiers.
FRAMEWORK_NOISE_WORDS = frozenset({
    "fromjson", "tojson", "setstate", "build", "initstate", "dispose",
    "copywith", "didchangedependencies", "didupdatewidget",
    "notifylisteners", "tostring", "hashcode",
})

# Explicit non-Dart file targets under lib/ (l10n .arb, config .json/.yaml,
# docs .md/.txt) are not Dart-source scope even though the path contains
# "lib" -- these must not fire.
NON_DART_EXTS = frozenset({"arb", "json", "yaml", "yml", "md", "txt"})

IDENT_BODY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")
CLASS_RE = re.compile(r"\bclass\s+([A-Za-z_]\w*)")
IMPLEMENTS_EXTENDS_RE = re.compile(r"\b(?:implements|extends)\s+([A-Za-z_]\w*)")
WITH_TYPE_RE = re.compile(r"\bwith\s+([A-Z]\w*)")

GREP_WORD_RE = re.compile(r"\b(?:grep|rg)\b")
# `\b` after every "dart" literal so ".dart_tool" (a real, common rg/grep
# exclude target) doesn't count as a Dart-scope signal (word chars include
# "_", so \b correctly refuses to match inside "dart_tool").
DART_SCOPE_FLAG_RE = re.compile(
    r"--include[= ]\*?\.dart\b"
    r"|--type[= ]dart\b"
    r"|--glob[= ]\*?\.dart\b"
    r"|-g\s*\*?\.dart\b"
    r"|-t\s*dart\b"
)
DART_EXT_TOKEN_RE = re.compile(r"\.dart$")

# Commands whose stdout a trailing grep is allowed to filter without losing
# "raw search over Dart source" status (per the report's precision row:
# flutter analyze/test piped into grep, or lark-cli piped into grep, are
# filtering a *tool's* output, not searching source, and must NOT fire).
OUTPUT_FILTER_ALLOWED_PREV_CMDS = frozenset({"cat", "find", "xargs"})

# grep/rg options that consume a separate following token as their value
# (as opposed to boolean flags like -n/-r/-l, or -e/-E which take no value
# of their own -- for -e the *next* token is the pattern itself, so it must
# fall through to the "first non-option token" return below, not be skipped
# here).
SHORT_OPTS_WITH_VALUE = frozenset({"-A", "-B", "-C", "-m", "-f", "-g", "-t"})
LONG_OPTS_WITH_VALUE = frozenset({"--include", "--exclude", "--type", "--glob", "--max-count"})

OP_INFO = {
    "findReferences": (
        "findReferences",
        "resolves imports/exports, no same-name noise",
        "findReferences at its definition",
    ),
    "goToImplementation": (
        "goToImplementation",
        "resolves real subtypes, no same-name noise",
        "goToImplementation on it",
    ),
    "goToDefinition": (
        "goToDefinition/workspaceSymbol",
        "jumps straight to the declaration, no scanning every hit",
        "goToDefinition or workspaceSymbol for it",
    ),
}

READ_OUTLINE_MIN_LINES = 250
SYMBOL_MESSAGE_MAX_LEN = 48

SESSION_START_CONTEXT = (
    "Dart/Flutter project: the native LSP tool is available (if it is not "
    "loaded yet, ToolSearch `select:LSP` once). For Dart symbols LSP beats "
    "grep: findReferences/incomingCalls resolve real usages (grep misses "
    "re-exports, extensions, part files, and conflates same-named symbols); "
    "goToDefinition/workspaceSymbol jump straight to a `class X` instead of "
    "scanning hits; documentSymbol gives a file's outline instead of "
    "reading it whole; goToImplementation finds real subtypes instead of "
    "grepping extends/implements/with.\n\n"
    "Don't skip LSP because \"the file is small\", \"grep is faster\", "
    "\"I already know the name\", \"it's one lookup\", or \"LSP isn't "
    "loaded yet\" (loading is one call). Grep is still right for: string "
    "literals, JSON/arb/l10n keys, comments/TODOs, non-Dart files, "
    "filtering another tool's output, and git refs not checked out."
)


# ---------------------------------------------------------------------------
# Dart-project detection
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


def _in_dart_project(data, candidate_path=None):
    """Decide Dart-project scope.

    When the tool call names an explicit target path (Read ``file_path``,
    Grep ``path``), scope is decided from that path alone -- a Dart ``cwd``
    must NOT paper over a target that lives outside any Dart project (e.g.
    a Grep/Read into a sibling non-Dart repo from a Dart-project cwd).
    ``cwd`` is only consulted when no target path was given at all (Bash,
    or a Grep call with no ``path``), since there is nothing else to scope
    from.
    """
    cwd = data.get("cwd") if isinstance(data, dict) else None
    if candidate_path:
        base = candidate_path
        if not os.path.isabs(base) and cwd:
            base = os.path.join(cwd, base)
        return bool(_find_pubspec_root(base))
    if isinstance(cwd, str) and _find_pubspec_root(cwd):
        return True
    return False


# ---------------------------------------------------------------------------
# Shell-command tokenizing: quote-aware split into pipeline/chain segments
# ---------------------------------------------------------------------------

def _split_shell(cmd):
    """Quote-aware split into a list of (separator_before, segment) pairs.

    Splits on unescaped &&, ||, ;, and | (outside single/double quotes);
    an escaped pipe (``\\|``) or a pipe inside quotes is left untouched in
    the segment text, since those are search-pattern alternation, not a
    shell pipe.
    """
    segments = []
    buf = []
    i = 0
    n = len(cmd)
    in_squote = False
    in_dquote = False
    cur_sep = ""
    while i < n:
        ch = cmd[i]
        if in_squote:
            buf.append(ch)
            if ch == "'":
                in_squote = False
            i += 1
            continue
        if in_dquote:
            buf.append(ch)
            if ch == '"':
                in_dquote = False
            i += 1
            continue
        if ch == "'":
            in_squote = True
            buf.append(ch)
            i += 1
            continue
        if ch == '"':
            in_dquote = True
            buf.append(ch)
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            buf.append(ch)
            buf.append(cmd[i + 1])
            i += 2
            continue
        two = cmd[i:i + 2]
        if two in ("&&", "||"):
            segments.append((cur_sep, "".join(buf)))
            buf = []
            cur_sep = two
            i += 2
            continue
        if ch in (";", "|"):
            segments.append((cur_sep, "".join(buf)))
            buf = []
            cur_sep = ch
            i += 1
            continue
        buf.append(ch)
        i += 1
    segments.append((cur_sep, "".join(buf)))
    return segments


def _tokenize_segment(text):
    """Quote-aware whitespace split of a single pipe-stage segment.

    Splits only on unescaped whitespace outside quotes, keeping any quote
    characters embedded in a token (e.g. ``--include='*.dart'`` stays a
    single token, not two) so option/value pairing below stays correct
    regardless of whether the value happens to be quoted.
    """
    tokens = []
    buf = []
    in_squote = False
    in_dquote = False
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if in_squote:
            buf.append(ch)
            if ch == "'":
                in_squote = False
            i += 1
            continue
        if in_dquote:
            buf.append(ch)
            if ch == '"':
                in_dquote = False
            i += 1
            continue
        if ch.isspace():
            if buf:
                tokens.append("".join(buf))
                buf = []
            i += 1
            continue
        if ch == "'":
            in_squote = True
            buf.append(ch)
            i += 1
            continue
        if ch == '"':
            in_dquote = True
            buf.append(ch)
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            buf.append(ch)
            buf.append(text[i + 1])
            i += 2
            continue
        buf.append(ch)
        i += 1
    if buf:
        tokens.append("".join(buf))
    return tokens


def _dequote_token(tok):
    if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in ("'", '"'):
        return tok[1:-1]
    return tok


def _group_clauses(segments):
    """Group (sep, seg) pairs into independent clauses split on &&/||/;.

    Each clause is itself a list of (sep, seg) pipe stages (first stage's
    sep is always "").
    """
    clauses = []
    cur = []
    for sep, seg in segments:
        if sep in ("", "&&", "||", ";"):
            if cur:
                clauses.append(cur)
            cur = [("", seg)]
        else:  # sep == "|"
            cur.append(("|", seg))
    if cur:
        clauses.append(cur)
    return clauses


def _is_git_segment(seg):
    """True if this pipe-stage's own command is a `git` invocation.

    Any git subcommand -- `git grep` (any ref, or none at all: HEAD~3,
    working tree, whatever), `git show <ref>:path`, `git log -S...`, `git
    ls-tree` -- means we're looking at history/refs, not live Dart source,
    so the whole clause is skipped. This subsumes the narrower "only
    origin/ refs" check it replaces: HEAD~3, arbitrary SHAs, and no ref at
    all (git grep over the working tree) are excluded just the same.
    """
    tokens = seg.strip().split()
    if not tokens:
        return False
    return tokens[0].rsplit("/", 1)[-1] == "git"


def _has_non_dart_ext(token):
    token = _dequote_token(token.strip())
    m = re.search(r"\.([A-Za-z0-9]+)$", token)
    return bool(m) and m.group(1).lower() in NON_DART_EXTS


def _is_dart_scoped_text(text):
    if DART_SCOPE_FLAG_RE.search(text):
        return True
    tokens = []
    for raw_tok in _tokenize_segment(text):
        # Strip a whole-token quote pair first (e.g. `"lib/foo.dart"`), then
        # any leftover boundary quote/punct from a value glued onto a flag
        # (e.g. `--include='*.dart'` tokenizes as one token with the quote
        # still attached at the end) so the suffix checks below see the
        # real trailing extension.
        tok = _dequote_token(raw_tok).strip(",;'\"")
        if tok:
            tokens.append(tok)
    # An explicit non-Dart target anywhere in the command (--include=*.json,
    # a bare lib/l10n/intl_en.arb positional, etc.) vetoes Dart scope for
    # the whole clause -- the caller told us exactly what it's targeting,
    # and a *different* token also running through lib/ (e.g. a search
    # directory positional alongside an --include filter) doesn't undo
    # that; see `settingsSchema` under lib/src/config --include=*.json.
    if any(_has_non_dart_ext(tok) for tok in tokens):
        return False
    for tok in tokens:
        if DART_EXT_TOKEN_RE.search(tok):
            return True
        if tok == "lib" or tok.startswith("lib/") or tok.endswith("/lib"):
            return True
    return False


def _extract_grep_pattern(segment_text):
    tokens = _tokenize_segment(segment_text)
    idx = None
    for i, tok in enumerate(tokens):
        if tok.rsplit("/", 1)[-1] in ("grep", "rg"):
            idx = i
            break
    if idx is None:
        return None
    i = idx + 1
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        if tok.startswith("--"):
            if "=" in tok:
                i += 1
                continue
            if tok in LONG_OPTS_WITH_VALUE:
                i += 2
                continue
            i += 1
            continue
        if tok.startswith("-") and len(tok) > 1:
            short = tok[:2]
            if short in SHORT_OPTS_WITH_VALUE:
                if len(tok) > 2:
                    i += 1  # value attached, e.g. -tdart, -A60
                else:
                    i += 2  # value is the next token, e.g. -A 5
                continue
            # boolean flag (-n, -r, -l, -i, -v, -E, combined -rln, ...),
            # or -e/-E: the *next* token is the pattern itself, so just
            # skip the flag and let the loop fall through to it.
            i += 1
            continue
        return _dequote_token(tok)
    return None


def _classify_clause(clause):
    if any(_is_git_segment(seg) for _, seg in clause):
        return None
    idx = None
    for i, (_, seg) in enumerate(clause):
        if GREP_WORD_RE.search(seg):
            idx = i
            break
    if idx is None:
        return None
    if idx > 0:
        prev_seg = clause[idx - 1][1].strip()
        prev_tokens = prev_seg.split()
        if not prev_tokens:
            return None
        base_cmd = prev_tokens[0].rsplit("/", 1)[-1]
        if base_cmd not in OUTPUT_FILTER_ALLOWED_PREV_CMDS:
            return None
    clause_text = " ".join(seg for _, seg in clause)
    if not _is_dart_scoped_text(clause_text):
        return None
    pattern_text = _extract_grep_pattern(clause[idx][1])
    if pattern_text is None:
        return None
    return classify_pattern(pattern_text)


def classify_bash_command(cmd):
    if not isinstance(cmd, str) or not GREP_WORD_RE.search(cmd):
        return None
    clauses = _group_clauses(_split_shell(cmd))
    for clause in clauses:
        result = _classify_clause(clause)
        if result:
            return result
    return None


# ---------------------------------------------------------------------------
# Pattern -> (op, symbol) classification, shared by Bash and native Grep
# ---------------------------------------------------------------------------

def _strip_regex_boundary(s):
    if s.startswith("\\b"):
        s = s[2:]
    if s.endswith("\\b"):
        s = s[:-2]
    return s


def _shape_identifier_core(a):
    if len(a) < 4:
        return None
    has_paren = False
    core = a
    if a.endswith("\\("):
        # escaped literal paren in an -E/extended-regex pattern, e.g.
        # `VoiceRecorder\(` for a call-shape search.
        has_paren = True
        core = a[:-2]
    elif a.endswith("("):
        has_paren = True
        core = a[:-1]
    if not core:
        return None
    starts_dot = core.startswith(".")
    body = core[1:] if starts_dot else core
    if not body or not IDENT_BODY_RE.match(body):
        return None
    last_seg = body.split(".")[-1]
    if last_seg.lower() in FRAMEWORK_NOISE_WORDS:
        # fromJson/toJson/setState/build/... are overridden everywhere;
        # findReferences noise, never nudge regardless of call-shape.
        return None
    generic = last_seg.lower() in GENERIC_WORDS
    if generic and not (has_paren or starts_dot):
        return None
    if has_paren or starts_dot:
        return core
    has_mixed_case = body.lower() != body and body.upper() != body
    if has_mixed_case:
        return core
    return None


def _alt_shape_identifier(alt):
    a = _strip_regex_boundary(alt.strip().strip("\"'"))
    result = _shape_identifier_core(a)
    if result is not None:
        return result
    # Fallback for a generic-looking prefix around a real identifier, e.g.
    # `Future<void> initAudio`: take the last identifier-like token and
    # validate that alone. Deliberately conservative -- if the tail token
    # doesn't pass the identifier rules either, we leave it unmatched
    # rather than guess.
    tail_tokens = [t for t in re.split(r"[^A-Za-z0-9_.()]+", a) if t]
    if tail_tokens and tail_tokens[-1] != a:
        return _shape_identifier_core(tail_tokens[-1])
    return None


def classify_pattern(pattern_text):
    if not isinstance(pattern_text, str) or not pattern_text.strip():
        return None
    normalized = pattern_text.replace("\\|", "|")
    m = IMPLEMENTS_EXTENDS_RE.search(normalized)
    if m:
        return ("goToImplementation", m.group(1))
    m = WITH_TYPE_RE.search(normalized)
    if m:
        return ("goToImplementation", m.group(1))
    m = CLASS_RE.search(normalized)
    if m:
        return ("goToDefinition", m.group(1))
    for alt in normalized.split("|"):
        sym = _alt_shape_identifier(alt)
        if sym:
            return ("findReferences", sym)
    return None


# ---------------------------------------------------------------------------
# Native Grep tool
# ---------------------------------------------------------------------------

def _grep_tool_scoped(tool_input):
    glob = tool_input.get("glob")
    type_ = tool_input.get("type")
    path = tool_input.get("path")
    # An explicit non-Dart glob/path (e.g. glob="*.arb", path="lib/l10n")
    # overrides any "lib" path heuristic below -- the caller told us
    # exactly what it's targeting, and it isn't Dart source.
    if isinstance(glob, str) and _has_non_dart_ext(glob):
        return False
    if isinstance(path, str) and _has_non_dart_ext(path):
        return False
    if isinstance(glob, str) and DART_EXT_TOKEN_RE.search(glob.lstrip("*")):
        return True
    if isinstance(type_, str) and type_.strip().lower() == "dart":
        return True
    if isinstance(path, str):
        p = path.replace("\\", "/")
        if p.endswith(".dart"):
            return True
        if "lib" in [seg for seg in p.split("/") if seg]:
            return True
    return False


def classify_grep_tool(tool_input):
    if not isinstance(tool_input, dict):
        return None
    pattern = tool_input.get("pattern")
    if not isinstance(pattern, str) or not pattern.strip():
        return None
    if not _grep_tool_scoped(tool_input):
        return None
    return classify_pattern(pattern)


# ---------------------------------------------------------------------------
# Read tool (outline suggestion)
# ---------------------------------------------------------------------------

NUL_SCAN_BYTES = 8192


def _looks_binary(path):
    try:
        with open(path, "rb") as f:
            chunk = f.read(NUL_SCAN_BYTES)
    except OSError:
        return True
    return b"\x00" in chunk


def _count_lines_at_least(path, threshold):
    try:
        count = 0
        with open(path, "r", errors="replace") as f:
            for _ in f:
                count += 1
                if count >= threshold:
                    return count
        return count
    except OSError:
        return None


def classify_read(tool_input):
    if not isinstance(tool_input, dict):
        return None
    fp = tool_input.get("file_path")
    if not isinstance(fp, str) or not fp.endswith(".dart"):
        return None
    if tool_input.get("offset") is not None or tool_input.get("limit") is not None:
        return None
    if not os.path.isfile(fp):
        return None
    if _looks_binary(fp):
        return None
    n = _count_lines_at_least(fp, READ_OUTLINE_MIN_LINES)
    if n is None or n < READ_OUTLINE_MIN_LINES:
        return None
    return fp, n


# ---------------------------------------------------------------------------
# Message building
# ---------------------------------------------------------------------------

def _truncate_symbol(sym):
    sym = sym.strip()
    if len(sym) > SYMBOL_MESSAGE_MAX_LEN:
        return sym[: SYMBOL_MESSAGE_MAX_LEN - 3] + "..."
    return sym


def build_symbol_message(op, symbol):
    label, reason, call_desc = OP_INFO.get(op, OP_INFO["findReferences"])
    sym = _truncate_symbol(symbol)
    return (
        "Dart symbol `{sym}`: LSP {label} is more precise than grep here "
        "({reason}). If LSP isn't loaded, ToolSearch `select:LSP`; then {call_desc}. Ignore if "
        "you're searching text, not the symbol."
    ).format(sym=sym, label=label, reason=reason, call_desc=call_desc)


def build_outline_message(file_path, line_count):
    # line_count comes from _count_lines_at_least, which stops counting at
    # READ_OUTLINE_MIN_LINES -- it is a floor, not the file's real length,
    # so the message must say "≥N", never claim N is the exact count.
    name = os.path.basename(file_path)
    return (
        "`{name}` is ≥{n} lines: LSP documentSymbol gives its outline "
        "(classes/methods) without reading the whole file. If LSP isn't "
        "loaded, ToolSearch `select:LSP`; then documentSymbol on it."
    ).format(name=name, n=line_count)


# ---------------------------------------------------------------------------
# Hook event handlers
# ---------------------------------------------------------------------------

def _emit(payload):
    sys.stdout.write(json.dumps(payload))


def _emit_pretooluse_context(message):
    _emit({
        "continue": True,
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": message,
        },
    })


def handle_session_start(data):
    cwd = data.get("cwd") if isinstance(data, dict) else None
    if not isinstance(cwd, str) or not _find_pubspec_root(cwd):
        return
    _emit({
        "continue": True,
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": SESSION_START_CONTEXT,
        },
    })


def handle_pre_tool_use(data):
    tool_name = data.get("tool_name") if isinstance(data, dict) else None
    if tool_name not in ("Bash", "Grep", "Read"):
        return
    tool_input = data.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = {}

    if tool_name == "Bash":
        if not _in_dart_project(data):
            return
        result = classify_bash_command(tool_input.get("command"))
        if not result:
            return
        _emit_pretooluse_context(build_symbol_message(*result))
        return

    if tool_name == "Grep":
        path = tool_input.get("path") if isinstance(tool_input.get("path"), str) else None
        if not _in_dart_project(data, path):
            return
        result = classify_grep_tool(tool_input)
        if not result:
            return
        _emit_pretooluse_context(build_symbol_message(*result))
        return

    if tool_name == "Read":
        fp = tool_input.get("file_path")
        candidate = os.path.dirname(fp) if isinstance(fp, str) else None
        if not _in_dart_project(data, candidate):
            return
        result = classify_read(tool_input)
        if not result:
            return
        _emit_pretooluse_context(build_outline_message(*result))
        return


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
    if event == "SessionStart":
        handle_session_start(data)
    elif event == "PreToolUse":
        handle_pre_tool_use(data)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
