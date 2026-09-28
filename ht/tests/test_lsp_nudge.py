#!/usr/bin/env python3
"""Hermetic tests for hooks/lsp_nudge.py.

Invokes the hook script as a subprocess (real stdin JSON in, real stdout
JSON out, real exit code) against temp directories, matching the actual
Claude Code hook contract rather than importing internals. Covers:

  * SessionStart inside/outside a Dart project.
  * Every PreToolUse positive shape (findReferences / goToDefinition /
    goToImplementation / documentSymbol-outline).
  * Every false-positive shape called out in
    .omc/research/lsp-missed-opportunities.md's "Mechanical detectability
    at PreToolUse time" precision row -- each must produce NO output.
  * git-ref archaeology commands -- NO output.
  * Subagent dispatches (any agent_type, incl. Explore) -- still nudged.
  * Malformed/empty/non-dict stdin -- exit 0, no output, never raises.
  * A frozen replay fixture of real audit commands (report-cited) plus
    synthetic variants covering the report's other precision-row false
    positive classes, with precision/recall printed and a >=90% precision
    gate.

Run: python3 ht/tests/test_lsp_nudge.py [-v]
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPT_PATH = os.path.normpath(os.path.join(THIS_DIR, "..", "..", "hooks", "lsp_nudge.py"))


def run_hook(payload, extra_env=None, timeout=5):
    env = os.environ.copy()
    env.pop("DART_LSP_NUDGE", None)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, SCRIPT_PATH],
        input=json.dumps(payload) if not isinstance(payload, str) else payload,
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
    )


def additional_context(proc):
    if not proc.stdout.strip():
        return None
    data = json.loads(proc.stdout)
    return data.get("hookSpecificOutput", {}).get("additionalContext")


def make_dart_project(root):
    os.makedirs(os.path.join(root, "lib"), exist_ok=True)
    with open(os.path.join(root, "pubspec.yaml"), "w") as f:
        f.write("name: fixture_project\nversion: 0.0.1\n")


def write_dart_file(path, line_count):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        for i in range(line_count):
            f.write("// line %d\n" % i)


class SessionStartTests(unittest.TestCase):
    def setUp(self):
        self.dart_root = tempfile.mkdtemp(prefix="lsp_nudge_dart_")
        make_dart_project(self.dart_root)
        self.plain_root = tempfile.mkdtemp(prefix="lsp_nudge_plain_")

    def tearDown(self):
        shutil.rmtree(self.dart_root, ignore_errors=True)
        shutil.rmtree(self.plain_root, ignore_errors=True)

    def test_fires_inside_dart_project(self):
        proc = run_hook({"hook_event_name": "SessionStart", "cwd": self.dart_root})
        self.assertEqual(proc.returncode, 0)
        ctx = additional_context(proc)
        self.assertIsNotNone(ctx, proc.stdout)
        self.assertIn("LSP", ctx)
        self.assertIn("grep", ctx)
        word_count = len(ctx.split())
        self.assertTrue(80 <= word_count <= 220, "word count %d out of expected band" % word_count)

    def test_fires_from_nested_subdirectory(self):
        nested = os.path.join(self.dart_root, "lib", "src", "ui")
        os.makedirs(nested, exist_ok=True)
        proc = run_hook({"hook_event_name": "SessionStart", "cwd": nested})
        self.assertEqual(proc.returncode, 0)
        self.assertIsNotNone(additional_context(proc))

    def test_silent_outside_dart_project(self):
        proc = run_hook({"hook_event_name": "SessionStart", "cwd": self.plain_root})
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), "")

    def test_kill_switch_disables(self):
        proc = run_hook(
            {"hook_event_name": "SessionStart", "cwd": self.dart_root},
            extra_env={"DART_LSP_NUDGE": "0"},
        )
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), "")


class PreToolUsePositiveShapeTests(unittest.TestCase):
    """One test per distinct op the classifier can emit."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="lsp_nudge_pos_")
        make_dart_project(self.root)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _bash(self, command):
        return run_hook({
            "hook_event_name": "PreToolUse",
            "cwd": self.root,
            "tool_name": "Bash",
            "tool_input": {"command": command},
        })

    def test_find_references_shape(self):
        proc = self._bash('grep -rn "buildKaraokeSession\\|ChatKaraokeSession" lib --include=*.dart')
        ctx = additional_context(proc)
        self.assertIsNotNone(ctx)
        self.assertIn("findReferences", ctx)
        self.assertIn("buildKaraokeSession", ctx)
        self.assertLessEqual(len(ctx.split()), 60)

    def test_go_to_definition_shape(self):
        proc = self._bash('grep -rn "class KaraokeOffsetMapper" -A60 lib | head -90')
        ctx = additional_context(proc)
        self.assertIsNotNone(ctx)
        self.assertIn("goToDefinition", ctx)
        self.assertIn("KaraokeOffsetMapper", ctx)

    def test_go_to_implementation_via_implements(self):
        proc = self._bash('grep -rln "implements TranslationProvider\\|extends TranslationProvider" lib')
        ctx = additional_context(proc)
        self.assertIsNotNone(ctx)
        self.assertIn("goToImplementation", ctx)
        self.assertIn("TranslationProvider", ctx)

    def test_go_to_implementation_via_with(self):
        proc = self._bash('grep -rln "with ChangeNotifier" lib --include=*.dart')
        ctx = additional_context(proc)
        self.assertIsNotNone(ctx)
        self.assertIn("goToImplementation", ctx)
        self.assertIn("ChangeNotifier", ctx)

    def test_native_grep_tool_find_references(self):
        proc = run_hook({
            "hook_event_name": "PreToolUse",
            "cwd": self.root,
            "tool_name": "Grep",
            "tool_input": {"pattern": "resolveTranslation|TranslationCache", "path": "lib/src/translation"},
        })
        ctx = additional_context(proc)
        self.assertIsNotNone(ctx)
        self.assertIn("findReferences", ctx)

    def test_native_grep_tool_go_to_definition(self):
        proc = run_hook({
            "hook_event_name": "PreToolUse",
            "cwd": self.root,
            "tool_name": "Grep",
            "tool_input": {"pattern": "class SessionRepository", "glob": "*.dart"},
        })
        ctx = additional_context(proc)
        self.assertIsNotNone(ctx)
        self.assertIn("goToDefinition", ctx)

    def test_native_grep_tool_go_to_implementation(self):
        proc = run_hook({
            "hook_event_name": "PreToolUse",
            "cwd": self.root,
            "tool_name": "Grep",
            "tool_input": {"pattern": "implements Comparable", "type": "dart"},
        })
        ctx = additional_context(proc)
        self.assertIsNotNone(ctx)
        self.assertIn("goToImplementation", ctx)

    def test_native_grep_tool_unscoped_is_silent(self):
        # No glob/type/path dart signal -- conservative no-fire even though
        # cwd is a Dart project (documented implementation contract).
        proc = run_hook({
            "hook_event_name": "PreToolUse",
            "cwd": self.root,
            "tool_name": "Grep",
            "tool_input": {"pattern": "class Foo"},
        })
        self.assertEqual(proc.stdout.strip(), "")

    def test_native_grep_tool_non_dart_ext_under_lib_is_silent(self):
        # path runs through lib/ and pattern looks identifier-shaped, but an
        # explicit .arb glob means the real target is l10n data, not Dart.
        proc = run_hook({
            "hook_event_name": "PreToolUse",
            "cwd": self.root,
            "tool_name": "Grep",
            "tool_input": {"pattern": "loginTitle", "path": "lib/l10n", "glob": "*.arb"},
        })
        self.assertEqual(proc.stdout.strip(), "")

    def test_native_grep_tool_arb_path_under_lib_is_silent(self):
        proc = run_hook({
            "hook_event_name": "PreToolUse",
            "cwd": self.root,
            "tool_name": "Grep",
            "tool_input": {"pattern": "loginTitle", "path": "lib/l10n/intl_en.arb"},
        })
        self.assertEqual(proc.stdout.strip(), "")

    def test_native_grep_tool_dart_tool_glob_is_not_dart_scope(self):
        # ".dart_tool" is a real rg/grep exclude glob; must not be treated
        # as a ".dart" substring match.
        proc = run_hook({
            "hook_event_name": "PreToolUse",
            "cwd": self.root,
            "tool_name": "Grep",
            "tool_input": {"pattern": "class Foo", "glob": "*.dart_tool"},
        })
        self.assertEqual(proc.stdout.strip(), "")

    def test_grep_scope_from_explicit_path_not_cwd_fallback(self):
        # cwd is a Dart project, but the Grep call's own `path` target is
        # not -- scope must be decided from the target path alone.
        non_dart = tempfile.mkdtemp(prefix="lsp_nudge_nondart_")
        try:
            proc = run_hook({
                "hook_event_name": "PreToolUse",
                "cwd": self.root,
                "tool_name": "Grep",
                "tool_input": {"pattern": "class Foo", "path": non_dart},
            })
            self.assertEqual(proc.stdout.strip(), "")
        finally:
            shutil.rmtree(non_dart, ignore_errors=True)

    def test_grep_scope_detected_from_path_when_cwd_is_not_dart(self):
        # The inverse: cwd is NOT a Dart project, but the explicit path
        # target is -- path alone must be sufficient.
        plain_cwd = tempfile.mkdtemp(prefix="lsp_nudge_plaincwd_")
        try:
            proc = run_hook({
                "hook_event_name": "PreToolUse",
                "cwd": plain_cwd,
                "tool_name": "Grep",
                "tool_input": {"pattern": "class Foo", "path": os.path.join(self.root, "lib")},
            })
            self.assertIsNotNone(additional_context(proc))
        finally:
            shutil.rmtree(plain_cwd, ignore_errors=True)

    def test_read_outline_shape(self):
        fp = os.path.join(self.root, "lib", "big.dart")
        write_dart_file(fp, 300)
        proc = run_hook({
            "hook_event_name": "PreToolUse",
            "cwd": self.root,
            "tool_name": "Read",
            "tool_input": {"file_path": fp},
        })
        ctx = additional_context(proc)
        self.assertIsNotNone(ctx)
        self.assertIn("documentSymbol", ctx)
        self.assertIn("big.dart", ctx)
        self.assertLessEqual(len(ctx.split()), 60)

    def test_read_outline_message_says_at_least_250(self):
        # Line counting stops at the threshold, so a 5000-line file still
        # counts out at exactly 250 -- the message must say "≥250", not
        # claim an exact count it never actually measured.
        fp = os.path.join(self.root, "lib", "huge.dart")
        write_dart_file(fp, 5000)
        proc = run_hook({
            "hook_event_name": "PreToolUse",
            "cwd": self.root,
            "tool_name": "Read",
            "tool_input": {"file_path": fp},
        })
        ctx = additional_context(proc)
        self.assertIsNotNone(ctx)
        self.assertIn("≥250", ctx)

    def test_read_skips_file_with_nul_in_first_8kb(self):
        fp = os.path.join(self.root, "lib", "weird.dart")
        os.makedirs(os.path.dirname(fp), exist_ok=True)
        with open(fp, "wb") as f:
            f.write(b"\x00")
            f.write(b"// line\n" * 300)
        proc = run_hook({
            "hook_event_name": "PreToolUse",
            "cwd": self.root,
            "tool_name": "Read",
            "tool_input": {"file_path": fp},
        })
        self.assertEqual(proc.stdout.strip(), "")

    def test_read_scope_from_file_path_not_cwd_fallback(self):
        # cwd is a Dart project, but the Read call's own file_path target
        # lives elsewhere -- scope must be decided from that path alone.
        non_dart = tempfile.mkdtemp(prefix="lsp_nudge_nondart_")
        try:
            fp = os.path.join(non_dart, "big.dart")
            write_dart_file(fp, 300)
            proc = run_hook({
                "hook_event_name": "PreToolUse",
                "cwd": self.root,
                "tool_name": "Read",
                "tool_input": {"file_path": fp},
            })
            self.assertEqual(proc.stdout.strip(), "")
        finally:
            shutil.rmtree(non_dart, ignore_errors=True)

    def test_read_small_file_is_silent(self):
        fp = os.path.join(self.root, "lib", "small.dart")
        write_dart_file(fp, 50)
        proc = run_hook({
            "hook_event_name": "PreToolUse",
            "cwd": self.root,
            "tool_name": "Read",
            "tool_input": {"file_path": fp},
        })
        self.assertEqual(proc.stdout.strip(), "")

    def test_read_with_limit_is_silent(self):
        fp = os.path.join(self.root, "lib", "big2.dart")
        write_dart_file(fp, 300)
        proc = run_hook({
            "hook_event_name": "PreToolUse",
            "cwd": self.root,
            "tool_name": "Read",
            "tool_input": {"file_path": fp, "limit": 100},
        })
        self.assertEqual(proc.stdout.strip(), "")

    def test_read_non_dart_file_is_silent(self):
        fp = os.path.join(self.root, "lib", "big.yaml")
        write_dart_file(fp, 300)
        proc = run_hook({
            "hook_event_name": "PreToolUse",
            "cwd": self.root,
            "tool_name": "Read",
            "tool_input": {"file_path": fp},
        })
        self.assertEqual(proc.stdout.strip(), "")

    def test_never_sets_permission_decision_or_denies(self):
        proc = self._bash('grep -rn "class KaraokeOffsetMapper" lib --include=*.dart')
        self.assertNotIn("permissionDecision", proc.stdout)
        self.assertNotIn("updatedInput", proc.stdout)


class ExploreSubagentTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="lsp_nudge_explore_")
        make_dart_project(self.root)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_explore_agent_still_nudged(self):
        # ~/.claude/agents/Explore.md grants LSP, so Explore can act on the nudge.
        proc = run_hook({
            "hook_event_name": "PreToolUse",
            "cwd": self.root,
            "tool_name": "Grep",
            "agent_type": "Explore",
            "tool_input": {"pattern": "class Foo", "glob": "*.dart"},
        })
        self.assertIsNotNone(additional_context(proc))

    def test_other_agent_types_still_fire(self):
        proc = run_hook({
            "hook_event_name": "PreToolUse",
            "cwd": self.root,
            "tool_name": "Bash",
            "agent_type": "code-reviewer",
            "tool_input": {"command": 'grep -rn "class KaraokeOffsetMapper" lib --include=*.dart'},
        })
        self.assertIsNotNone(additional_context(proc))


class MalformedInputTests(unittest.TestCase):
    def test_invalid_json(self):
        proc = run_hook("not json at all")
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), "")
        self.assertEqual(proc.stderr.strip(), "")

    def test_empty_stdin(self):
        proc = run_hook("")
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), "")

    def test_json_array_not_object(self):
        proc = run_hook("[1, 2, 3]")
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), "")

    def test_unknown_hook_event(self):
        proc = run_hook({"hook_event_name": "SomeFutureEvent", "cwd": "/tmp"})
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), "")

    def test_missing_hook_event_name(self):
        proc = run_hook({"cwd": "/tmp"})
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), "")

    def test_pretooluse_missing_tool_input(self):
        proc = run_hook({"hook_event_name": "PreToolUse", "cwd": "/tmp", "tool_name": "Bash"})
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), "")

    def test_pretooluse_non_string_command(self):
        proc = run_hook({
            "hook_event_name": "PreToolUse", "cwd": "/tmp", "tool_name": "Bash",
            "tool_input": {"command": 12345},
        })
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), "")

    def test_irrelevant_tool_name_silent(self):
        proc = run_hook({
            "hook_event_name": "PreToolUse", "cwd": "/tmp", "tool_name": "Write",
            "tool_input": {"file_path": "/tmp/x.dart", "content": "class Foo {}"},
        })
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), "")


# ---------------------------------------------------------------------------
# Replay fixture: real audit commands (hand-labeled, citing the report) plus
# synthetic variants covering the report's other documented false-positive
# classes. Each tuple is (command, expect_fire, source_note).
# ---------------------------------------------------------------------------

FIXTURE = [
    # --- report-cited positives (cat1 find-usages) ---
    (
        'grep -rn "KaraokeOffsetMapper.map\\|startOffset\\|endOffset" lib --include=*.dart | grep -v karaoke_offset_mapper.dart | head -20',
        True,
        "report cat1 example 1: subagents/agent-ace81b46c85fea73c.jsonl:46",
    ),
    (
        'grep -n "buildKaraokeSession\\|ChatKaraokeSession\\|KaraokeOffsetMapper\\|shouldClearKaraokeSessionOnCompleted" -r lib/src --include=*.dart',
        True,
        "report cat1 example 2: subagents/agent-a34df0601d6a1ac4e.jsonl:13",
    ),
    (
        'grep -n "playPosition\\|playSegmentIndex" lib/src/ui/chat/view_model.dart | head -20',
        True,
        "report cat1 example 3: 0559752f-...jsonl:6370",
    ),
    # --- report-cited positives (cat2 find-definition) ---
    (
        'grep -rn "class KaraokeOffsetMapper" -A60 lib | head -90',
        True,
        "report cat2 example 1: 0559752f-...jsonl:3217",
    ),
    (
        'grep -n "_updateMessageRemote(" -A12 lib/src/ui/chat/view_model.dart',
        True,
        "report cat2 example 2: 0559752f-...jsonl:4485",
    ),
    (
        'grep -rn "class WordTranslationText" . 2>/dev/null | grep -v ".dart_tool"',
        False,
        "report cat2 example 3 (subagents/agent-a34df0601d6a1ac4e.jsonl:284) relabeled "
        "honestly: the old detector fired here, but only because \"\\.dart\" in text "
        "matched the substring inside \".dart_tool\" -- a false positive in the "
        "detector's own reasoning, not a real Dart-scope signal. The command has no "
        "--include=*.dart, no --type dart, and no lib/ path token (target is \".\"); "
        "fixed to require \".dart\" as a real token suffix (see DART_EXT_TOKEN_RE), "
        "this command now correctly has no fire, at the cost of this one recall point.",
    ),
    # --- report-cited positives (cat5 implements/extends) ---
    (
        'grep -rln "implements TranslationProvider\\|extends TranslationProvider" lib',
        True,
        "report cat5 example 1: subagents/agent-a4afa21f60850d3f3.jsonl:263",
    ),
    (
        'grep -rln "implements TranslationProvider\\|TranslationProvider(" lib',
        True,
        "report cat5 example 2 (bare 'lib', no trailing slash -- the documented scope undercount fix): subagents/agent-a4afa21f60850d3f3.jsonl:264",
    ),
    # --- report-cited false positives (must NOT fire) ---
    (
        'flutter analyze lib 2>&1 | grep -E "error|update_message_api|no_such_method"',
        False,
        "report precision row: grep filtering flutter analyze's own output",
    ),
    (
        'flutter test test/foo_test.dart 2>&1 | grep -E "Error|No named parameter"',
        False,
        "report precision row: grep filtering flutter test's own output",
    ),
    (
        'git checkout HEAD -- lib/src/ui/chat/view_model.dart && lark-cli send --room x | grep -E "message_id|error"',
        False,
        "report precision row: grep filters lark-cli output; only matched the old classifier via an unrelated .dart in an adjacent compound command",
    ),
    (
        'grep -rn "login\\|Login" lib --include=*.dart',
        False,
        "report precision row: too generic, high collision risk",
    ),
    (
        'grep -rn "word_boundaries\\|audio_offset_ms\\|text_offset\\|word_length" lib --include=*.dart',
        False,
        "derived from the audit report's precision-row description (not a verbatim "
        "transcript line -- no cited jsonl offset): snake_case JSON payload keys, not "
        "Dart identifiers (variant 1)",
    ),
    (
        'grep -rn "word_length\\|audio_offset_ms" lib --include=*.dart',
        False,
        "derived from the audit report's precision-row description (not a verbatim "
        "transcript line -- no cited jsonl offset): snake_case JSON payload keys, not "
        "Dart identifiers (variant 2)",
    ),
    # --- report-cited git-ref archaeology (must NOT fire) ---
    (
        'cd ~/Desktop/projects/ht_speak_buddy && git grep -n "class SettingsLogic" origin/develop/6.4.30 --',
        False,
        "report: git-ref archaeology excluded population (hellotalk-module Explore subagents)",
    ),
    (
        'cd /Users/ives/Desktop/projects/hello_words_package && git show origin/main/6.4.30:lib/src/models/hw_home.dart | grep -n "class\\|topic\\|category\\|banner" -i',
        False,
        "report: git-ref archaeology excluded population",
    ),
    # --- synthetic positives (additional shapes not in the report) ---
    ('grep -rn "onMessageReceived" lib/src/chat --include=*.dart', True, "synthetic: plain camelCase findReferences"),
    ('grep -rn "extends Equatable" lib --include=*.dart', True, "synthetic: extends"),
    ('rg "workspaceSymbolCache" lib --type dart', True, "synthetic: rg + --type dart scope flag"),
    ('grep -rn "isLoading\\|isPlaying\\|isPaused" lib/src/state --include=*.dart', True, "synthetic: multiple camelCase alternatives"),
    ('grep -rn "AppColors.primary\\|AppColors.secondary" lib/src/theme --include=*.dart', True, "synthetic: dotted qualified access, mixed-case qualifier"),
    ('grep -rln "with ChangeNotifier" lib --include=*.dart', True, "synthetic: with-mixin"),
    ('grep -rn "buildWidgetTree(" lib --include=*.dart', True, "synthetic: call-shape identifier"),
    ('find lib -name "*.dart" | xargs grep -n "ChatMessageBubble"', True, "synthetic: xargs is an allowed output-filter predecessor"),
    ('cat lib/src/ui/chat/view_model.dart | grep -n "PlaybackController"', True, "synthetic: cat is an allowed output-filter predecessor"),
    (
        'grep -n "class _ChatViewState extends State<ChatView>" lib/src/ui/chat/chat_view.dart',
        True,
        "synthetic: ambiguous class+extends shape, op unpinned but must fire",
    ),
    # --- synthetic negatives (additional false-positive classes) ---
    ('grep -rn "TODO" lib --include=*.dart', False, "synthetic: bare all-caps word is not camelCase/PascalCase shape"),
    ('grep -rn "login\\|password" lib --include=*.dart', False, "synthetic: generic stoplisted + plain lowercase word"),
    ('grep -rn "hello world" lib --include=*.dart', False, "synthetic: free-text phrase, not an identifier"),
    ('grep -n "TODO: fix race condition" lib/src/foo.dart', False, "synthetic: comment/TODO text search"),
    ('grep -rn "id\\|key" lib --include=*.dart', False, "synthetic: alternatives below length-4 floor"),
    ('grep -rn "wordBoundary" test/fixtures/sample.json', False, "synthetic: identifier shape but not Dart-scoped (json file, no lib/.dart)"),
    ('grep -rn "className" README.md', False, "synthetic: non-Dart file, no lib scope"),
    ('git log --oneline -- lib/src/ui/chat/view_model.dart', False, "synthetic: no grep/rg at all"),
    ('grep -c "class" lib --include=*.dart', False, "synthetic: bare 'class' keyword, stoplisted, no following type name"),
    ('grep -rn "TODO(ives)" lib --include=*.dart', False, "synthetic: parenthesized comment marker, not a call-shape identifier"),
    ('grep -rn "primary\\|secondary" lib/src/theme --include=*.dart', False, "synthetic: generic lowercase words, no Dart shape"),
    ('grep -rn "AudioPlayerController" lib --include=*.dart', True, "synthetic: single PascalCase alternative"),
    ('grep -rn "userId\\|sessionId" lib --include=*.dart', True, "synthetic: camelCase alternatives despite an 'Id' suffix"),
    ('grep -rn "flutter_bloc" lib --include=*.dart', False, "synthetic: snake_case package name, not a Dart identifier"),
    ('grep -rn "isDartScoped" pubspec.yaml', False, "synthetic: identifier shape but target file is not lib/.dart scoped"),

    # --- reviewer round: non-Dart target under lib/ must not fire (item 2) ---
    (
        'grep -n loginButtonTitle lib/l10n/intl_en.arb',
        False,
        "reviewer FP: explicit .arb target under lib/ is l10n data, not Dart source",
    ),
    (
        'grep -rn "settingsSchema" lib/src/config --include=*.json',
        False,
        "reviewer FP: --include=*.json explicitly scopes to JSON, not Dart, even though the path runs through lib/",
    ),

    # --- reviewer round: git skip must cover any ref, not just origin/ (item 3) ---
    (
        'git grep -n "class SettingsLogic" HEAD~3 --',
        False,
        "reviewer FP: git grep with a relative ref (HEAD~3, no origin/) -- old detector only excluded origin/ refs",
    ),
    (
        'git show HEAD~2:lib/src/foo.dart | grep -n "class Foo"',
        False,
        "reviewer FP: git show on a non-origin ref piped to grep",
    ),
    (
        'git log -S"VoiceRecorder" -- lib/src/foo.dart',
        False,
        "reviewer FP: git log -S pickaxe search, no grep/rg word at all (sanity check for the general git-segment skip)",
    ),

    # --- reviewer round: framework/lifecycle stoplist (item 4) ---
    ('grep -rn "dispose" lib --include=*.dart', False, "reviewer FP: dispose() is overridden everywhere, findReferences noise"),
    ('grep -rn "notifyListeners()" lib --include=*.dart', False, "reviewer FP: notifyListeners call-shape still noisy, stoplisted regardless of parens"),
    ('grep -n "toJson()" lib/src/foo.dart', False, "reviewer FP: toJson() is boilerplate on every model class"),
    ('grep -rn "build" lib --include=*.dart', False, "reviewer FP: build() override, stoplisted even as a bare (non-call) alternative"),
    (
        'grep -rn "dispose\\|MyCustomThing" lib --include=*.dart',
        True,
        "reviewer FP follow-up: stoplist only suppresses the noisy alternative, a real identifier alongside it still fires",
    ),

    # --- reviewer round: pattern-extraction recall (item 5) ---
    (
        "grep -rn --include='*.dart' VoiceRecorder .",
        True,
        "recall: options before the pattern, --include value single-quoted and glued to '=' (item 5 worked example)",
    ),
    ('grep -A 5 "class VoiceRecorder" lib --include=*.dart', True, "recall: -A N (separate-value context option) before the quoted pattern is irrelevant here, but must not break extraction when it follows"),
    ('grep -B 2 "VoiceRecorder" lib --include=*.dart', True, "recall: -B N separate-value option"),
    ('grep -C 3 "VoiceRecorder" lib --include=*.dart', True, "recall: -C N separate-value option"),
    ('grep -m 10 "VoiceRecorder" lib --include=*.dart', True, "recall: -m N separate-value option (max-count)"),
    ('grep -e VoiceRecorder lib --include=*.dart', True, "recall: -e PAT -- PAT is the pattern itself, not a value to skip"),
    ('grep -f patterns.txt lib --include=*.dart', False, "recall: -f FILE reads patterns from a file -- no literal pattern text on the command line; documented as OK to leave unmatched"),
    ("grep -rn --include '*.dart' VoiceRecorder lib", True, "recall: --include PAT with a separate (non '=') value token"),
    ("rg -g '*.dart' VoiceRecorder lib", True, "recall: rg -g glob (separate value) before the pattern"),
    ("rg -t dart VoiceRecorder lib", True, "recall: rg -t dart (space-separated) establishes Dart scope and is skipped as a flag+value pair"),
    ("rg --type dart VoiceRecorder lib", True, "recall: rg --type dart (space-separated long form)"),
    ("rg --type=dart VoiceRecorder lib", True, "recall: rg --type=dart ('=' form)"),
    ("rg -tdart VoiceRecorder lib", True, "recall: rg -tdart (glued short form, no separator)"),
    (r"rg '\bVoiceRecorder\b' lib", True, "recall: \\b word-boundary regex markers around the identifier must be stripped before shape-checking"),
    (r"grep -E 'VoiceRecorder\(' lib", True, "recall: -E extended regex with an escaped literal paren, call-shape"),
    (
        'grep -rn "Future<void> initAudio" lib --include=*.dart',
        True,
        "recall: generic-prefix fallback -- `Future<void> initAudio` isn't itself identifier-shaped, but its last token `initAudio` is; documented fallback, OK to leave unmatched if the tail token is ambiguous",
    ),
]


class ReplayFixtureTest(unittest.TestCase):
    """Reproduces (in miniature) lsp-missed-opportunities.py's replay: runs
    the real detector over a frozen, hand-labeled command set and asserts
    a precision floor, printing the confusion detail either way."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="lsp_nudge_replay_")
        make_dart_project(self.root)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_fixture_precision_and_recall(self):
        self.assertGreaterEqual(len(FIXTURE), 40, "fixture must have >= 40 entries")

        tp = fp = tn = fn = 0
        fp_list = []
        fn_list = []

        for command, expect_fire, source in FIXTURE:
            proc = run_hook({
                "hook_event_name": "PreToolUse",
                "cwd": self.root,
                "tool_name": "Bash",
                "tool_input": {"command": command},
            })
            fired = bool(additional_context(proc))
            if expect_fire and fired:
                tp += 1
            elif expect_fire and not fired:
                fn += 1
                fn_list.append((command, source))
            elif not expect_fire and fired:
                fp += 1
                fp_list.append((command, source, additional_context(proc)))
            else:
                tn += 1

        precision = tp / (tp + fp) if (tp + fp) else float("nan")
        recall = tp / (tp + fn) if (tp + fn) else float("nan")

        report = textwrap.dedent("""
            === LSP nudge replay fixture ===
            n={n}  TP={tp} FP={fp} TN={tn} FN={fn}
            precision={precision:.3f}  recall={recall:.3f}
        """).format(n=len(FIXTURE), tp=tp, fp=fp, tn=tn, fn=fn, precision=precision, recall=recall)
        if fp_list:
            report += "\nFalse positives (fired but should not have):\n"
            for cmd, source, ctx in fp_list:
                report += "  - {source}\n    cmd: {cmd}\n    ctx: {ctx}\n".format(source=source, cmd=cmd, ctx=ctx)
        if fn_list:
            report += "\nFalse negatives (should have fired but did not):\n"
            for cmd, source in fn_list:
                report += "  - {source}\n    cmd: {cmd}\n".format(source=source, cmd=cmd)
        print(report)

        self.assertGreaterEqual(precision, 0.90, "precision below 90% floor:\n" + report)


if __name__ == "__main__":
    unittest.main(verbosity=2)
