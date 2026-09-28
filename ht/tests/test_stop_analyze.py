#!/usr/bin/env python3
"""Hermetic tests for hooks/stop_analyze.py.

Invokes the hook script as a subprocess (real stdin JSON in, real stdout
JSON out, real exit code) against a temp Dart project with a fake `dart`
SDK, matching the actual Claude Code hook contract rather than importing
internals -- same pattern as test_dart_lsp_idle.py / test_lsp_nudge.py.

The fake `dart` (installed as the FVM "default" SDK so bin/dart-lsp's own
selection logic resolves it deterministically, with no real Dart install
required) understands files dropped into its cwd by each test:
`.fake_analyze_output` (canned `dart analyze --format=machine` stdout),
`.fake_analyze_sleep` (seconds to sleep first, for the timeout case),
`.fake_analyze_exit_code` (exit code to return, default 0), and always
records its own argv to `.fake_analyze_argv` so tests can assert whether
`dart analyze` ran at all for a given root and turn.

Covers: PostToolUse recording scope (Dart file, inside a Dart project,
a tracked tool only -- NotebookEdit is deliberately NOT tracked);
Stop/SubagentStop's fingerprint-based repeat-check semantics (a fresh
block, a genuinely NEW error on the stop_hook_active retry still
blocking, the SAME error persisting instead passing and clearing state,
and no `dart analyze` call at all on a later turn with no new edits);
edited-file-first ordering and the 30-line cap; warnings/infos-only
clearing state; a missing `.dart_tool/package_config.json` skipping a
root unless it's a pub workspace member; an analyze exit 64, and an SDK
resolution failure, each skipping a root without blocking or clearing on
a first check; the asymmetric skip handling on a stop_hook_active
retry -- a still-skipped root must NOT veto releasing the scope once
every remaining error was already reported in a prior round; an overall
timeout clearing state; nested sub-package diagnostics being excluded
from an outer-package check; background (still-running) subagents being
invisible to the main thread's Stop; the missing-agent_id SubagentStop
no-op; the kill switch; and malformed stdin.

Run: python3 -m unittest ht.tests.test_stop_analyze [-v]
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.normpath(os.path.join(THIS_DIR, "..", ".."))
SCRIPT_PATH = os.path.join(REPO_ROOT, "hooks", "stop_analyze.py")

# A fake `dart`: for `analyze --format=machine`, records its own argv (so
# tests can assert whether `dart analyze` ran at all, and with what
# arguments -- e.g. confirming the fixed "." invocation now that the old
# per-root narrowing is gone), optionally sleeps (for the timeout case),
# then cats a canned machine-format fixture from its cwd if one was
# dropped there, and exits with a configurable code (default 0). Any
# other invocation is a silent no-op success -- bin/dart-lsp's own
# DART_LSP_PRINT=1 path never actually execs this binary, it only stats
# it for executability, so nothing else needs to be handled.
FAKE_DART_SRC = """#!/bin/sh
if [ "$1" = "analyze" ]; then
  shift
  printf '%s\\n' "$*" > ./.fake_analyze_argv
  if [ -f "./.fake_analyze_sleep" ]; then
    sleep "$(cat ./.fake_analyze_sleep)"
  fi
  if [ -f "./.fake_analyze_output" ]; then
    cat "./.fake_analyze_output"
  fi
  if [ -f "./.fake_analyze_exit_code" ]; then
    exit "$(cat ./.fake_analyze_exit_code)"
  fi
  exit 0
fi
exit 0
"""


def _write_executable(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)
    os.chmod(path, 0o755)


class Harness(unittest.TestCase):
    """Hermetic $HOME / $FVM_CACHE_PATH / $CLAUDE_PLUGIN_DATA sandbox with a
    fake FVM "default" Dart SDK, so bin/dart-lsp resolves it without any
    real Dart install (no .fvmrc / .fvm in any fixture project, so the
    FVM-default branch is always the one that fires -- see test_dart_lsp.sh
    "default SDK without .fvmrc")."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = self._tmp.name
        self.addCleanup(self._tmp.cleanup)

        self.home = os.path.join(self.tmp_path, "home")
        self.fvm_cache = os.path.join(self.tmp_path, "fvm")
        self.plugin_data = os.path.join(self.tmp_path, "plugin-data")
        os.makedirs(self.home, exist_ok=True)

        self.fake_dart = os.path.join(
            self.fvm_cache, "default", "bin", "cache", "dart-sdk", "bin", "dart"
        )
        _write_executable(self.fake_dart, FAKE_DART_SRC)

        self.env = os.environ.copy()
        self.env.pop("DART_STOP_ANALYZE", None)
        self.env.pop("DART_STOP_ANALYZE_TIMEOUT", None)
        self.env["HOME"] = self.home
        self.env["FVM_CACHE_PATH"] = self.fvm_cache
        self.env["CLAUDE_PLUGIN_ROOT"] = REPO_ROOT
        self.env["CLAUDE_PLUGIN_DATA"] = self.plugin_data

    # -- fixture builders --------------------------------------------------

    def make_project(self, name, with_package_config=True):
        root = os.path.join(self.tmp_path, name)
        os.makedirs(os.path.join(root, "lib"), exist_ok=True)
        with open(os.path.join(root, "pubspec.yaml"), "w") as f:
            f.write("name: %s\nversion: 0.0.1\n" % name.replace("-", "_"))
        if with_package_config:
            os.makedirs(os.path.join(root, ".dart_tool"), exist_ok=True)
            with open(os.path.join(root, ".dart_tool", "package_config.json"), "w") as f:
                f.write('{"configVersion":2,"packages":[]}')
        return root

    def write_dart_file(self, root, rel_path, content="void main() {}\n"):
        path = os.path.join(root, rel_path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(content)
        return path

    def set_analyze_output(self, root, lines):
        with open(os.path.join(root, ".fake_analyze_output"), "w") as f:
            f.write("\n".join(lines) + ("\n" if lines else ""))

    def set_analyze_sleep(self, root, seconds):
        with open(os.path.join(root, ".fake_analyze_sleep"), "w") as f:
            f.write(str(seconds))

    def set_analyze_exit_code(self, root, code):
        with open(os.path.join(root, ".fake_analyze_exit_code"), "w") as f:
            f.write(str(code))

    def make_nested_package(self, root, rel_dir, name):
        """A nested Dart package (its own pubspec.yaml) living inside
        `root`, e.g. an `example/` app -- must never be scoped into an
        analysis run triggered by an edit in the outer package."""
        nested_root = os.path.join(root, rel_dir)
        os.makedirs(os.path.join(nested_root, "lib"), exist_ok=True)
        with open(os.path.join(nested_root, "pubspec.yaml"), "w") as f:
            f.write("name: %s\nversion: 0.0.1\n" % name)
        return nested_root

    def make_workspace_root(self, name, members):
        """A pub workspace root: `.dart_tool/package_config.json` lives
        only here (pub workspaces resolve deps once, at the root), and
        its pubspec.yaml declares `workspace:`."""
        root = os.path.join(self.tmp_path, name)
        os.makedirs(os.path.join(root, ".dart_tool"), exist_ok=True)
        member_list = "\n".join("  - %s" % m for m in members)
        with open(os.path.join(root, "pubspec.yaml"), "w") as f:
            f.write("name: %s\nversion: 0.0.1\nworkspace:\n%s\n" % (name.replace("-", "_"), member_list))
        with open(os.path.join(root, ".dart_tool", "package_config.json"), "w") as f:
            f.write('{"configVersion":2,"packages":[]}')
        return root

    def make_workspace_member(self, workspace_root, rel_dir, name):
        """A workspace member package: its own pubspec.yaml declaring
        `resolution: workspace`, deliberately with NO `.dart_tool` of its
        own -- that's the whole point of a workspace."""
        member_root = os.path.join(workspace_root, rel_dir)
        os.makedirs(os.path.join(member_root, "lib"), exist_ok=True)
        with open(os.path.join(member_root, "pubspec.yaml"), "w") as f:
            f.write("name: %s\nversion: 0.0.1\nresolution: workspace\n" % name)
        return member_root

    def read_analyze_argv(self, root):
        path = os.path.join(root, ".fake_analyze_argv")
        if not os.path.exists(path):
            return None
        with open(path) as f:
            return f.read().split()

    def clear_analyze_argv(self, root):
        path = os.path.join(root, ".fake_analyze_argv")
        if os.path.exists(path):
            os.remove(path)

    # -- hook invocation -----------------------------------------------

    def run_hook(self, payload, extra_env=None, timeout=15):
        env = dict(self.env)
        if extra_env:
            env.update(extra_env)
        stdin = payload if isinstance(payload, str) else json.dumps(payload)
        return subprocess.run(
            [sys.executable, SCRIPT_PATH],
            input=stdin,
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout,
        )

    def post_edit(self, session_id, file_path, agent_id=None, cwd=None, tool_name="Edit"):
        payload = {
            "hook_event_name": "PostToolUse",
            "session_id": session_id,
            "tool_name": tool_name,
            "tool_input": {"file_path": file_path},
        }
        if agent_id is not None:
            payload["agent_id"] = agent_id
        if cwd is not None:
            payload["cwd"] = cwd
        proc = self.run_hook(payload)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "", "PostToolUse must never emit output")
        return proc

    def stop(self, session_id, stop_hook_active=False, cwd=None, agent_id=None, event="Stop",
              extra_env=None):
        payload = {
            "hook_event_name": event,
            "session_id": session_id,
            "stop_hook_active": stop_hook_active,
        }
        if cwd is not None:
            payload["cwd"] = cwd
        if agent_id is not None:
            payload["agent_id"] = agent_id
        return self.run_hook(payload, extra_env=extra_env)

    def broken_sdk_env(self):
        """Env overrides that make bin/dart-lsp's own SDK resolution fail
        for every root (no FVM default, no PATH dart, no project
        .fvmrc) -- mirrors test_dart_lsp.sh's "missing SDK exits 127"
        case, used here to simulate an SDK-resolve failure distinct from
        `dart analyze` itself returning a bad exit code."""
        return {
            "FVM_CACHE_PATH": os.path.join(self.tmp_path, "no-fvm-here"),
            "PATH": "/nonexistent-bin-dir",
        }

    # -- state inspection ------------------------------------------------

    def state_file(self, session_id):
        return os.path.join(self.plugin_data, "stop-analyze", session_id + ".jsonl")

    def read_raw_lines(self, session_id):
        path = self.state_file(session_id)
        if not os.path.exists(path):
            return []
        lines = []
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line:
                    lines.append(json.loads(line))
        return lines

    def read_state_entries(self, session_id):
        return [o for o in self.read_raw_lines(session_id) if "path" in o]

    def read_finished_agents(self, session_id):
        return set(
            o["finished_agent_id"] for o in self.read_raw_lines(session_id)
            if isinstance(o.get("finished_agent_id"), str)
        )


class PostToolUseTests(Harness):
    def test_records_dart_file_in_project(self):
        root = self.make_project("proj-record")
        f = self.write_dart_file(root, "lib/foo.dart")
        session = "sess-record"
        self.post_edit(session, f)
        entries = self.read_state_entries(session)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["path"], f)
        self.assertIsNone(entries[0]["agent_id"])

    def test_ignores_dart_file_outside_dart_project(self):
        outside_dir = os.path.join(self.tmp_path, "not-a-dart-project")
        os.makedirs(outside_dir, exist_ok=True)
        f = os.path.join(outside_dir, "foo.dart")
        with open(f, "w") as fh:
            fh.write("void main() {}\n")
        session = "sess-outside"
        self.post_edit(session, f)
        self.assertEqual(self.read_state_entries(session), [])

    def test_ignores_non_dart_file(self):
        root = self.make_project("proj-nondart")
        f = os.path.join(root, "pubspec.yaml")
        session = "sess-nondart"
        self.post_edit(session, f)
        self.assertEqual(self.read_state_entries(session), [])

    def test_ignores_untracked_tool(self):
        root = self.make_project("proj-untracked")
        f = self.write_dart_file(root, "lib/foo.dart")
        session = "sess-untracked-tool"
        proc = self.run_hook({
            "hook_event_name": "PostToolUse",
            "session_id": session,
            "tool_name": "Read",
            "tool_input": {"file_path": f},
        })
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(self.read_state_entries(session), [])

    def test_ignores_notebook_edit(self):
        # NotebookEdit never carries a .dart path (it's notebook_path,
        # cell-scoped) and was dropped from the tracked-tool/hook matcher
        # entirely -- confirm it's inert even if fed a .dart file_path.
        root = self.make_project("proj-notebook")
        f = self.write_dart_file(root, "lib/foo.dart")
        session = "sess-notebook"
        self.post_edit(session, f, tool_name="NotebookEdit")
        self.assertEqual(self.read_state_entries(session), [])

    def test_records_agent_id_when_present(self):
        root = self.make_project("proj-agentid")
        f = self.write_dart_file(root, "lib/foo.dart")
        session = "sess-agentid"
        self.post_edit(session, f, agent_id="agent-xyz")
        entries = self.read_state_entries(session)
        self.assertEqual(entries[0]["agent_id"], "agent-xyz")


class StopRepeatCheckTests(Harness):
    """The core P1 semantics: every Stop/SubagentStop -- including the
    automatic stop_hook_active retry -- actually re-analyzes, diffing
    against a persisted per-scope "already reported" fingerprint set."""

    def test_no_edits_is_silent(self):
        proc = self.stop("sess-noedits")
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")

    def test_fresh_block_persists_blocked_set(self):
        root = self.make_project("proj-fresh")
        f = self.write_dart_file(root, "lib/foo.dart")
        session = "sess-fresh"
        self.post_edit(session, f)
        self.set_analyze_output(root, [
            "ERROR|COMPILE_TIME_ERROR|UNDEFINED_METHOD|%s|3|5|3|Error A." % f,
        ])
        proc = self.stop(session, stop_hook_active=False, cwd=root)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["decision"], "block")
        self.assertIn("Error A", payload["reason"])
        # A block never clears -- the next Stop (active or not) re-checks.
        self.assertEqual(len(self.read_state_entries(session)), 1)

    def test_stop_hook_active_without_prior_round_still_analyzes(self):
        # An unusual but possible shape: stop_hook_active=True with no
        # earlier block from this hook in this scope (empty blocked-set).
        # Must still actually analyze and block -- not silently trust the
        # flag the way the old "if stop_hook_active: return" did.
        root = self.make_project("proj-active-first")
        f = self.write_dart_file(root, "lib/foo.dart")
        session = "sess-active-first"
        self.post_edit(session, f)
        self.set_analyze_output(root, [
            "ERROR|COMPILE_TIME_ERROR|UNDEFINED_METHOD|%s|3|5|3|The method 'bar' isn't defined." % f,
        ])
        proc = self.stop(session, stop_hook_active=True, cwd=root)
        self.assertEqual(proc.returncode, 0)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["decision"], "block")
        self.assertIn("bar", payload["reason"])

    def test_stop_hook_active_new_error_blocks(self):
        # Round 1 blocks on error A. The "fix" replaces A with a
        # different error B -- B was never reported before, so round 2
        # (stop_hook_active) must still block, listing B.
        root = self.make_project("proj-new-error")
        f = self.write_dart_file(root, "lib/foo.dart")
        session = "sess-new-error"
        self.post_edit(session, f)
        self.set_analyze_output(root, [
            "ERROR|COMPILE_TIME_ERROR|UNDEFINED_METHOD|%s|3|5|3|Error A." % f,
        ])
        proc1 = self.stop(session, stop_hook_active=False, cwd=root)
        self.assertEqual(json.loads(proc1.stdout)["decision"], "block")

        self.set_analyze_output(root, [
            "ERROR|COMPILE_TIME_ERROR|UNDEFINED_METHOD|%s|9|1|3|Error B." % f,
        ])
        proc2 = self.stop(session, stop_hook_active=True, cwd=root)
        self.assertNotEqual(proc2.stdout, "", "a genuinely new error must still block")
        payload2 = json.loads(proc2.stdout)
        self.assertEqual(payload2["decision"], "block")
        self.assertIn("Error B", payload2["reason"])
        self.assertNotIn("Error A", payload2["reason"], "already-reported A must not be re-listed")
        # Still blocked -> state stays around for a possible round 3.
        self.assertEqual(len(self.read_state_entries(session)), 1)

    def test_stop_hook_active_persisting_error_passes_and_clears(self):
        # Round 1 blocks on error A. Round 2 (stop_hook_active), A is
        # still present, unchanged -- already reported once, so this
        # must pass silently and clear state (not nag forever).
        root = self.make_project("proj-persist")
        f = self.write_dart_file(root, "lib/foo.dart")
        session = "sess-persist"
        self.post_edit(session, f)
        self.set_analyze_output(root, [
            "ERROR|COMPILE_TIME_ERROR|UNDEFINED_METHOD|%s|3|5|3|Error A." % f,
        ])
        proc1 = self.stop(session, stop_hook_active=False, cwd=root)
        self.assertEqual(json.loads(proc1.stdout)["decision"], "block")

        proc2 = self.stop(session, stop_hook_active=True, cwd=root)
        self.assertEqual(proc2.stdout, "", "an already-reported, still-present error must not re-block")
        self.assertEqual(self.read_state_entries(session), [])

    def test_no_edits_after_pass_skips_analyze_entirely(self):
        # After a clean pass+clear, a later Stop with nothing new edited
        # must never even invoke `dart analyze` again -- proven via the
        # fake dart's argv log rather than just "no output" (which could
        # also happen if analyze ran and simply found nothing).
        root = self.make_project("proj-no-reanalyze")
        f = self.write_dart_file(root, "lib/foo.dart")
        session = "sess-no-reanalyze"
        self.post_edit(session, f)
        self.set_analyze_output(root, [])  # clean
        proc1 = self.stop(session, stop_hook_active=False, cwd=root)
        self.assertEqual(proc1.stdout, "")
        self.assertIsNotNone(self.read_analyze_argv(root), "first Stop should have run analyze")
        self.assertEqual(self.read_state_entries(session), [])

        self.clear_analyze_argv(root)
        proc2 = self.stop(session, stop_hook_active=False, cwd=root)
        self.assertEqual(proc2.returncode, 0)
        self.assertEqual(proc2.stdout, "")
        self.assertIsNone(
            self.read_analyze_argv(root),
            "no new edits this turn -> dart analyze must not run at all",
        )

    def test_errors_block_with_edited_first_and_cap(self):
        root = self.make_project("proj-cap")
        edited = self.write_dart_file(root, "lib/edited.dart")
        other = self.write_dart_file(root, "lib/other.dart")
        session = "sess-cap"
        self.post_edit(session, edited)

        lines = []
        for i in range(5):
            lines.append(
                "ERROR|COMPILE_TIME_ERROR|UNDEFINED_METHOD|%s|%d|1|3|edited error %d."
                % (edited, i + 1, i)
            )
        for i in range(30):
            lines.append(
                "ERROR|COMPILE_TIME_ERROR|UNDEFINED_IDENTIFIER|%s|%d|1|3|other error %d."
                % (other, i + 1, i)
            )
        self.set_analyze_output(root, lines)

        proc = self.stop(session, stop_hook_active=False, cwd=root)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["decision"], "block")
        self.assertIn("Fix these before finishing", payload["reason"])

        reason_lines = payload["reason"].split("\n")
        error_section = reason_lines[:30]
        self.assertEqual(len(error_section), 30, error_section)
        for line in error_section[:5]:
            self.assertIn("edited.dart", line)
        for line in error_section[5:29]:
            self.assertIn("other.dart", line)
        self.assertEqual(error_section[29], "+6 more")
        self.assertEqual(reason_lines[30], "")
        self.assertIn("Fix these before finishing", reason_lines[31])
        self.assertIn(
            "If an error pre-existed and is unrelated to your change, say so and stop again.",
            reason_lines[31],
        )

        # Errors found -> entries kept so the next Stop re-checks.
        self.assertEqual(len(self.read_state_entries(session)), 1)

    def test_only_warnings_is_silent_and_clears_state(self):
        root = self.make_project("proj-warn")
        f = self.write_dart_file(root, "lib/foo.dart")
        session = "sess-warn"
        self.post_edit(session, f)
        self.set_analyze_output(root, [
            "WARNING|STATIC_WARNING|UNUSED_IMPORT|%s|1|1|3|Unused import." % f,
            "INFO|LINT|PREFER_CONST|%s|2|1|3|Prefer const." % f,
        ])
        proc = self.stop(session, stop_hook_active=False)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")
        self.assertEqual(self.read_state_entries(session), [])

    def test_missing_package_config_is_silent(self):
        root = self.make_project("proj-nopkg", with_package_config=False)
        f = self.write_dart_file(root, "lib/foo.dart")
        session = "sess-nopkg"
        self.post_edit(session, f)
        # Even though this ERROR is planted, the root must be skipped
        # entirely because deps were never installed -- if it weren't
        # skipped, this test would (correctly) see a block.
        self.set_analyze_output(root, [
            "ERROR|COMPILE_TIME_ERROR|UNDEFINED_METHOD|%s|3|5|3|The method 'bar' isn't defined." % f,
        ])
        proc = self.stop(session, stop_hook_active=False)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")

    def test_analyze_exit_64_skips_without_clear_or_block(self):
        # A usage-error exit (64) means the invocation itself was
        # rejected -- not "clean" and not "found errors". Must neither
        # block nor clear state.
        root = self.make_project("proj-exit64")
        f = self.write_dart_file(root, "lib/foo.dart")
        session = "sess-exit64"
        self.post_edit(session, f)
        self.set_analyze_output(root, [
            "ERROR|COMPILE_TIME_ERROR|UNDEFINED_METHOD|%s|3|5|3|Should be ignored." % f,
        ])
        self.set_analyze_exit_code(root, 64)
        proc = self.stop(session, stop_hook_active=False, cwd=root)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "", "exit 64 must not block")
        self.assertEqual(len(self.read_state_entries(session)), 1, "exit 64 must not clear state")

    def test_sdk_resolve_failure_skips_without_clear_or_block(self):
        # An SDK-resolve failure (bin/dart-lsp itself can't find a Dart
        # SDK at all -- no FVM default, no PATH dart, no project
        # .fvmrc) happens before `dart analyze` even gets a chance to
        # run, but must be treated the same as an untrustworthy analyze
        # exit: skip this root, don't block, don't clear.
        root = self.make_project("proj-sdk-fail")
        f = self.write_dart_file(root, "lib/foo.dart")
        session = "sess-sdk-fail"
        self.post_edit(session, f)
        proc = self.stop(session, stop_hook_active=False, cwd=root, extra_env=self.broken_sdk_env())
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "", "an SDK-resolve failure must not block")
        self.assertEqual(
            len(self.read_state_entries(session)), 1, "an SDK-resolve failure must not clear state"
        )

    def test_stop_hook_active_releases_despite_skipped_root(self):
        # Two roots in the same turn: one (proj-skip) always returns
        # analyze exit 64 (skip, never contributes errors either way);
        # the other (proj-persist) has a real error that survives
        # unchanged into the stop_hook_active retry. Turn 1 blocks on
        # the real error. The retry must pass AND fully clear -- the
        # skipped root must not veto the release just because the
        # scope's report is empty only because of it, since every
        # fingerprint left in E was already reported once (round 1).
        root_skip = self.make_project("proj-skip")
        skip_file = self.write_dart_file(root_skip, "lib/skip.dart")
        self.set_analyze_exit_code(root_skip, 64)

        root_persist = self.make_project("proj-persist2")
        persist_file = self.write_dart_file(root_persist, "lib/persist.dart")
        self.set_analyze_output(root_persist, [
            "ERROR|COMPILE_TIME_ERROR|UNDEFINED_METHOD|%s|3|5|3|Error A." % persist_file,
        ])

        session = "sess-skip-and-persist"
        self.post_edit(session, skip_file)
        self.post_edit(session, persist_file)

        proc1 = self.stop(session, stop_hook_active=False)
        payload1 = json.loads(proc1.stdout)
        self.assertEqual(payload1["decision"], "block", "turn 1 must block on the real, persisting error")
        self.assertIn("Error A", payload1["reason"])

        proc2 = self.stop(session, stop_hook_active=True)
        self.assertEqual(
            proc2.stdout, "",
            "retry must pass even though proj-skip is still being skipped this round",
        )
        self.assertEqual(
            self.read_state_entries(session), [],
            "retry must fully clear state, not leave it dangling because of the skipped root",
        )

        # Next turn, no new edits at all -- neither root's `dart analyze`
        # should run.
        self.clear_analyze_argv(root_skip)
        self.clear_analyze_argv(root_persist)
        proc3 = self.stop(session, stop_hook_active=False)
        self.assertEqual(proc3.returncode, 0)
        self.assertEqual(proc3.stdout, "")
        self.assertIsNone(self.read_analyze_argv(root_skip))
        self.assertIsNone(self.read_analyze_argv(root_persist))

    def test_timeout_clears_state(self):
        root = self.make_project("proj-timeout")
        f = self.write_dart_file(root, "lib/foo.dart")
        session = "sess-timeout"
        self.post_edit(session, f)
        self.set_analyze_sleep(root, 3)
        start = time.time()
        proc = self.stop(
            session, stop_hook_active=False, cwd=root,
        ) if False else self.run_hook(
            {
                "hook_event_name": "Stop",
                "session_id": session,
                "stop_hook_active": False,
                "cwd": root,
            },
            extra_env={"DART_STOP_ANALYZE_TIMEOUT": "1"},
        )
        elapsed = time.time() - start
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")
        self.assertLess(elapsed, 10, "hook should give up around the 1s timeout, not wait out the sleep")
        # A check we couldn't complete releases the scope rather than
        # dangling forever -- see the module docstring's "Timeouts".
        self.assertEqual(self.read_state_entries(session), [])

    def test_malformed_stdin_is_silent(self):
        proc = self.run_hook("not json {{{")
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")

    def test_kill_switch_disables_recording(self):
        root = self.make_project("proj-kill")
        f = self.write_dart_file(root, "lib/foo.dart")
        session = "sess-kill"
        proc = self.run_hook(
            {
                "hook_event_name": "PostToolUse",
                "session_id": session,
                "tool_name": "Edit",
                "tool_input": {"file_path": f},
            },
            extra_env={"DART_STOP_ANALYZE": "0"},
        )
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(self.read_state_entries(session), [])


class NestedSubpackageScopeTests(Harness):
    """A package root's `dart analyze` can still walk into a nested
    package's directory (its own `pubspec.yaml`, e.g. `example/`, deps
    never installed there) and report diagnostics for it. Those must never
    count toward blocking a turn on an outer-package edit -- this is the
    real bug the ht_speak_buddy real-world check turned up (75 false
    errors from example/). The old per-root "narrowing" (excluding a
    nested package's directory from the analyze invocation) was removed
    on review (measured to add nothing); the authoritative filter is
    purely post-parse, by each diagnostic's own nearest pubspec.yaml
    root."""

    def test_nested_package_errors_excluded_root_errors_still_block(self):
        root = self.make_project("proj-nested")
        root_file = self.write_dart_file(root, "lib/foo.dart")
        nested_root = self.make_nested_package(root, "example", "example")
        nested_file = self.write_dart_file(nested_root, "lib/bar.dart")
        session = "sess-nested"
        self.post_edit(session, root_file)

        self.set_analyze_output(root, [
            "ERROR|COMPILE_TIME_ERROR|UNDEFINED_METHOD|%s|3|5|3|The method 'bar' isn't defined."
            % root_file,
            "ERROR|COMPILE_TIME_ERROR|URI_DOES_NOT_EXIST|%s|1|1|3|"
            "Target of URI doesn't exist: 'package:whatever/whatever.dart'." % nested_file,
        ])

        proc = self.stop(session, stop_hook_active=False, cwd=root)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["decision"], "block")
        self.assertIn("foo.dart", payload["reason"])
        self.assertNotIn("bar.dart", payload["reason"])
        self.assertNotIn("example", payload["reason"])

        # Narrowing was removed on review: always the whole-directory
        # invocation now, regardless of what's nested inside.
        self.assertEqual(self.read_analyze_argv(root), ["--format=machine", "."])

    def test_nested_package_only_error_is_silent_and_clears_state(self):
        root = self.make_project("proj-nested-only")
        root_file = self.write_dart_file(root, "lib/foo.dart")
        nested_root = self.make_nested_package(root, "example", "example")
        nested_file = self.write_dart_file(nested_root, "lib/bar.dart")
        session = "sess-nested-only"
        self.post_edit(session, root_file)

        self.set_analyze_output(root, [
            "ERROR|COMPILE_TIME_ERROR|URI_DOES_NOT_EXIST|%s|1|1|3|"
            "Target of URI doesn't exist: 'package:whatever/whatever.dart'." % nested_file,
        ])

        proc = self.stop(session, stop_hook_active=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "")
        self.assertEqual(self.read_state_entries(session), [])


class WorkspaceTests(Harness):
    def test_workspace_member_finds_ancestor_package_config(self):
        workspace_root = self.make_workspace_root("proj-workspace", members=["packages/foo"])
        member_root = self.make_workspace_member(workspace_root, "packages/foo", "foo")
        member_file = self.write_dart_file(member_root, "lib/bar.dart")
        session = "sess-workspace"
        self.post_edit(session, member_file)

        # The member itself has NO .dart_tool -- only the workspace root
        # does. Without ancestor-workspace-root detection this root would
        # be silently skipped (treated as "deps never installed").
        self.assertFalse(
            os.path.exists(os.path.join(member_root, ".dart_tool")),
            "fixture sanity: workspace members never get their own .dart_tool",
        )

        self.set_analyze_output(member_root, [
            "ERROR|COMPILE_TIME_ERROR|UNDEFINED_METHOD|%s|3|5|3|Error in workspace member." % member_file,
        ])
        proc = self.stop(session, stop_hook_active=False, cwd=member_root)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["decision"], "block", "workspace member must not be silently skipped")
        self.assertIn("Error in workspace member", payload["reason"])
        # `dart analyze` ran with the member's own directory as cwd.
        self.assertEqual(self.read_analyze_argv(member_root), ["--format=machine", "."])


class SubagentStopScopeTests(Harness):
    def test_finished_subagent_absorbed_by_main_still_running_excluded(self):
        root_a = self.make_project("proj-a")
        root_b = self.make_project("proj-b")
        root_m = self.make_project("proj-m")
        file_a = self.write_dart_file(root_a, "lib/a.dart")
        file_b = self.write_dart_file(root_b, "lib/b.dart")
        file_m = self.write_dart_file(root_m, "lib/m.dart")
        session = "sess-scope"

        self.post_edit(session, file_a, agent_id="agent-A")
        self.post_edit(session, file_b, agent_id="agent-B")
        self.post_edit(session, file_m)  # main thread, agent_id: null

        # root A is clean; root B has a fresh error; root M is clean.
        self.set_analyze_output(root_a, [])
        self.set_analyze_output(root_b, [
            "ERROR|COMPILE_TIME_ERROR|UNDEFINED_METHOD|%s|1|1|3|The method 'x' isn't defined." % file_b,
        ])
        self.set_analyze_output(root_m, [])

        # Agent A passes (clean) -> its own scope is released: blocked-set
        # cleared, a finished marker recorded, but its EDIT ENTRY STAYS
        # (so the main thread can later absorb and itself re-check it).
        proc_a = self.stop(session, stop_hook_active=False, agent_id="agent-A", event="SubagentStop")
        self.assertEqual(proc_a.returncode, 0)
        self.assertEqual(proc_a.stdout, "", "agent A's SubagentStop must not see root B's errors")
        entries_after_a = self.read_state_entries(session)
        self.assertEqual(
            {e["path"] for e in entries_after_a}, {file_a, file_b, file_m},
            "agent A's entry must persist after it passes, not be deleted",
        )
        self.assertEqual(self.read_finished_agents(session), {"agent-A"})

        # Agent B is still running (no SubagentStop of its own yet). A
        # main Stop right now must be invisible to B's edits entirely --
        # neither analyzing root B nor clearing/blocking on it -- while
        # still picking up its own (agent_id=None) edits AND agent A's
        # now-finished ones.
        self.clear_analyze_argv(root_b)
        proc_main = self.stop(session, stop_hook_active=False, cwd=root_m)
        self.assertEqual(proc_main.returncode, 0)
        self.assertEqual(
            proc_main.stdout, "",
            "main Stop must not block on a still-running subagent's (agent B's) error",
        )
        self.assertIsNone(
            self.read_analyze_argv(root_b),
            "main Stop must not even run `dart analyze` on a still-running subagent's root",
        )

        # Main's own scope (agent_id None + finished agent A) is now
        # clean and released: its own entry and agent A's absorbed entry
        # (plus A's finished marker) are cleared. Agent B's entry, never
        # in scope, is untouched.
        remaining = self.read_state_entries(session)
        self.assertEqual({e["path"] for e in remaining}, {file_b})
        self.assertEqual(remaining[0]["agent_id"], "agent-B")
        self.assertEqual(self.read_finished_agents(session), set(), "A's marker is consumed once absorbed")

        # Agent B finally finishes its own turn and still blocks on its
        # own (still-present) error.
        proc_b = self.stop(session, stop_hook_active=False, agent_id="agent-B", event="SubagentStop")
        payload_b = json.loads(proc_b.stdout)
        self.assertEqual(payload_b["decision"], "block")
        self.assertIn("b.dart", payload_b["reason"])

    def test_subagent_stop_hook_active_new_error_blocks(self):
        root = self.make_project("proj-agent-retry")
        f = self.write_dart_file(root, "lib/foo.dart")
        session = "sess-agent-retry"
        self.post_edit(session, f, agent_id="agent-X")
        self.set_analyze_output(root, [
            "ERROR|COMPILE_TIME_ERROR|UNDEFINED_METHOD|%s|3|5|3|Error A." % f,
        ])
        proc1 = self.stop(session, stop_hook_active=False, cwd=root, agent_id="agent-X", event="SubagentStop")
        self.assertEqual(json.loads(proc1.stdout)["decision"], "block")

        self.set_analyze_output(root, [
            "ERROR|COMPILE_TIME_ERROR|UNDEFINED_METHOD|%s|9|1|3|Error B." % f,
        ])
        proc2 = self.stop(session, stop_hook_active=True, cwd=root, agent_id="agent-X", event="SubagentStop")
        payload2 = json.loads(proc2.stdout)
        self.assertEqual(payload2["decision"], "block")
        self.assertIn("Error B", payload2["reason"])

    def test_missing_agent_id_is_a_noop(self):
        # Not every SubagentStop comes from a subagent Claude spawned --
        # Claude Code's own internal agents (prompt suggestions, /btw side
        # questions) fire it too and, per the hooks docs, may carry no
        # agent_id. Such an event must not touch the main thread's (or any
        # other agent's) recorded edits.
        root = self.make_project("proj-noagentid")
        f = self.write_dart_file(root, "lib/foo.dart")
        session = "sess-noagentid"
        self.post_edit(session, f)  # main-thread edit -> agent_id: null
        self.set_analyze_output(root, [
            "ERROR|COMPILE_TIME_ERROR|UNDEFINED_METHOD|%s|3|5|3|The method 'bar' isn't defined." % f,
        ])

        proc_missing = self.stop(session, stop_hook_active=False, event="SubagentStop")
        self.assertEqual(proc_missing.returncode, 0)
        self.assertEqual(
            proc_missing.stdout, "", "SubagentStop without agent_id must be a no-op"
        )

        proc_empty = self.stop(session, stop_hook_active=False, agent_id="", event="SubagentStop")
        self.assertEqual(proc_empty.returncode, 0)
        self.assertEqual(
            proc_empty.stdout, "", "SubagentStop with an empty agent_id must also be a no-op"
        )

        # Neither call analyzed or cleared anything.
        entries = self.read_state_entries(session)
        self.assertEqual(len(entries), 1)
        self.assertIsNone(entries[0]["agent_id"])


if __name__ == "__main__":
    unittest.main()
