#!/usr/bin/env python3
"""Hermetic tests for bin/dart-lsp-idle and the idle-routing branch of
bin/dart-lsp. Spawns a fake LSP server (also written to a temp dir by
this file) so no real Dart SDK is required.
"""
import base64
import json
import os
import queue
import signal
import stat
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PROXY = REPO_ROOT / "bin" / "dart-lsp-idle"
DART_LSP_LAUNCHER = REPO_ROOT / "bin" / "dart-lsp"

TIMEOUT = object()

# A fake LSP server: answers requests with an echo result (deliberately
# odd JSON whitespace, to prove the proxy forwards bytes unchanged
# rather than re-serializing), understands shutdown/exit, and can log
# every raw message it sees/sends plus its own pid for the tests to
# inspect.
FAKE_SERVER_SRC = r'''#!/usr/bin/env python3
import base64, json, os, sys, threading, time

def read_raw_message(stream):
    headers = b""
    while True:
        line = stream.readline()
        if not line:
            return None
        headers += line
        if line in (b"\r\n", b"\n"):
            break
    content_length = None
    for raw in headers.split(b"\r\n"):
        name, sep, value = raw.partition(b":")
        if sep and name.strip().lower() == b"content-length":
            content_length = int(value.strip())
    body = b""
    if content_length:
        while len(body) < content_length:
            chunk = stream.read(content_length - len(body))
            if not chunk:
                break
            body += chunk
    return headers + body

def log(tag, raw):
    log_path = os.environ.get("FAKE_LSP_LOG")
    if not log_path:
        return
    with open(log_path, "a") as f:
        f.write(tag + ":" + base64.b64encode(raw).decode("ascii") + "\n")

write_lock = threading.Lock()

def write_raw(raw):
    with write_lock:
        log("SENT", raw)
        sys.stdout.buffer.write(raw)
        sys.stdout.buffer.flush()

pidfile = os.environ.get("FAKE_LSP_PIDFILE")
if pidfile:
    with open(pidfile, "w") as f:
        f.write(str(os.getpid()))

delay = float(os.environ.get("FAKE_LSP_DELAY", "0"))
spam_interval = float(os.environ.get("FAKE_LSP_SPAM_INTERVAL", "0"))

def spam():
    # Unsolicited server->client notifications, the way a real analysis
    # server fires publishDiagnostics/$/analyzerStatus on every disk
    # change regardless of whether the client asked for anything.
    n = 0
    while True:
        time.sleep(spam_interval)
        n += 1
        note = json.dumps({"jsonrpc": "2.0", "method": "$/fakeSpam", "params": {"n": n}}).encode()
        write_raw(("Content-Length: %d\r\n\r\n" % len(note)).encode() + note)

if spam_interval > 0:
    threading.Thread(target=spam, daemon=True).start()

def handle_echo(mid):
    # Runs in its own thread so a slow request never blocks the main
    # read loop from seeing (and answering) shutdown/exit promptly --
    # mirroring a real language server, where "in flight" is a genuine
    # race the proxy has to guard against, not an artifact of a
    # single-threaded fake.
    if delay:
        time.sleep(delay)
    resp = ('{"jsonrpc":   "2.0",  "id":  %s,   "result":  {"echo":   true} }' % json.dumps(mid)).encode()
    write_raw(("Content-Length: %d\r\n\r\n" % len(resp)).encode() + resp)

while True:
    raw = read_raw_message(sys.stdin.buffer)
    if raw is None:
        break
    log("RECV", raw)
    header_end = raw.index(b"\r\n\r\n") + 4
    body = raw[header_end:]
    try:
        msg = json.loads(body)
    except ValueError:
        continue
    method = msg.get("method")
    mid = msg.get("id")
    if method == "exit":
        break
    if method == "shutdown":
        shutdown_delay = float(os.environ.get("FAKE_LSP_SHUTDOWN_DELAY", "0"))
        if shutdown_delay:
            time.sleep(shutdown_delay)
        resp = json.dumps({"jsonrpc": "2.0", "id": mid, "result": None}).encode()
        write_raw(("Content-Length: %d\r\n\r\n" % len(resp)).encode() + resp)
        continue
    if mid is not None:
        threading.Thread(target=handle_echo, args=(mid,), daemon=True).start()

sys.exit(0)
'''

# A fake `dart` binary for exercising bin/dart-lsp's routing decision:
# it just records its own pid/ppid/argv and exits immediately. Plain
# POSIX sh, not Python -- a real `dart` binary has no python3 dependency
# at all, and tests that shadow python3 on PATH (e.g. a broken stub)
# must not accidentally break this fake too via its own shebang.
FAKE_DART_SRC = r'''#!/bin/sh
if [ -n "${FAKE_DART_RESULT:-}" ]; then
  ppid=$(ps -o ppid= -p $$ 2>/dev/null | tr -d ' ')
  printf 'pid=%s ppid=%s argv=%s\n' "$$" "$ppid" "$*" > "$FAKE_DART_RESULT"
fi
exit 0
'''


def write_executable(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def read_exact(stream, n):
    buf = b""
    while len(buf) < n:
        chunk = stream.read(n - len(buf))
        if not chunk:
            break
        buf += chunk
    return buf


def read_raw_message(stream):
    headers = b""
    while True:
        line = stream.readline()
        if not line:
            return None, None
        headers += line
        if line in (b"\r\n", b"\n"):
            break
    content_length = None
    for raw in headers.split(b"\r\n"):
        name, sep, value = raw.partition(b":")
        if sep and name.strip().lower() == b"content-length":
            content_length = int(value.strip())
    body = read_exact(stream, content_length) if content_length else b""
    return headers + body, body


def build_raw_request(mid, method, params):
    # Deliberately odd whitespace: proves the proxy forwards the exact
    # bytes it received instead of re-serializing the parsed JSON.
    body = ('{"jsonrpc":  "2.0",   "id":  %d,  "method":  %s,   "params":  %s}' % (
        mid, json.dumps(method), json.dumps(params))).encode()
    header = ("Content-Length: %d\r\n\r\n" % len(body)).encode()
    return header + body


def parse_log(log_path):
    entries = []
    if not os.path.exists(log_path):
        return entries
    with open(log_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            tag, _, b64 = line.partition(":")
            entries.append((tag, base64.b64decode(b64)))
    return entries


def raw_method(raw):
    header_end = raw.index(b"\r\n\r\n") + 4
    return json.loads(raw[header_end:]).get("method")


class Proxy:
    """Drives bin/dart-lsp-idle wrapping the fake LSP server."""

    def __init__(self, tmp_path, idle_seconds, fake_delay=None, pidfile=False, spam_interval=None,
                 shutdown_delay=None):
        self.tmp_path = tmp_path
        self.server_path = tmp_path / "fake-lsp-server.py"
        write_executable(self.server_path, FAKE_SERVER_SRC)
        self.log_path = str(tmp_path / "fake-lsp.log")
        self.pidfile_path = str(tmp_path / "fake-lsp.pid") if pidfile else None
        self.start_time = time.time()

        env = dict(os.environ)
        env["FAKE_LSP_LOG"] = self.log_path
        if fake_delay is not None:
            env["FAKE_LSP_DELAY"] = str(fake_delay)
        if self.pidfile_path:
            env["FAKE_LSP_PIDFILE"] = self.pidfile_path
        if spam_interval is not None:
            env["FAKE_LSP_SPAM_INTERVAL"] = str(spam_interval)
        if shutdown_delay is not None:
            env["FAKE_LSP_SHUTDOWN_DELAY"] = str(shutdown_delay)

        self.proc = subprocess.Popen(
            [sys.executable, str(PROXY), str(idle_seconds), sys.executable, str(self.server_path)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env,
        )
        self._next_id = 1
        self.out_queue = queue.Queue()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self):
        while True:
            raw, body = read_raw_message(self.proc.stdout)
            if raw is None:
                self.out_queue.put(None)
                return
            parsed = None
            if body:
                try:
                    parsed = json.loads(body)
                except ValueError:
                    parsed = None
            self.out_queue.put((raw, parsed))

    def read_message(self, timeout=5):
        try:
            item = self.out_queue.get(timeout=timeout)
        except queue.Empty:
            return TIMEOUT
        return item

    def next_id(self):
        mid = self._next_id
        self._next_id += 1
        return mid

    def send_request(self, method, params=None):
        mid = self.next_id()
        raw = build_raw_request(mid, method, params or {})
        self.proc.stdin.write(raw)
        self.proc.stdin.flush()
        return mid, raw

    def close(self, timeout=10):
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        self._close_pipes()

    def force_kill(self):
        if self.proc.poll() is None:
            try:
                self.proc.kill()
                self.proc.wait(timeout=5)
            except Exception:
                pass
        self._close_pipes()

    def _close_pipes(self):
        for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            try:
                stream.close()
            except OSError:
                pass


class DartLspIdleTest(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir.name)
        self.addCleanup(self._tmpdir.cleanup)

    def make_proxy(self, idle_seconds, fake_delay=None, pidfile=False, spam_interval=None,
                   shutdown_delay=None):
        proxy = Proxy(self.tmp_path, idle_seconds, fake_delay=fake_delay, pidfile=pidfile,
                       spam_interval=spam_interval, shutdown_delay=shutdown_delay)
        self.addCleanup(proxy.force_kill)
        return proxy

    # (a) byte-exact passthrough of a request/response round trip.
    def test_a_byte_exact_passthrough(self):
        proxy = self.make_proxy(idle_seconds=60)
        mid, raw_req = proxy.send_request("test/echo", {"foo": "bar"})

        raw_resp, parsed = proxy.read_message(timeout=5)
        self.assertIsNotNone(parsed, "expected a parsed response")
        self.assertEqual(parsed.get("id"), mid)
        self.assertEqual(parsed.get("result"), {"echo": True})

        entries = parse_log(proxy.log_path)
        self.assertEqual(entries[0], ("RECV", raw_req),
                          "server must see the exact bytes the client sent")
        self.assertEqual(entries[1], ("SENT", raw_resp),
                          "client must see the exact bytes the server sent")
        proxy.close()

    # (b) exits after idle (idle_seconds=1) with fake server receiving
    # shutdown then exit.
    def test_b_exits_after_idle(self):
        proxy = self.make_proxy(idle_seconds=1)
        returncode = proxy.proc.wait(timeout=10)
        self.assertEqual(returncode, 0)
        entries = parse_log(proxy.log_path)
        recv_methods = [raw_method(raw) for tag, raw in entries if tag == "RECV"]
        self.assertEqual(recv_methods, ["shutdown", "exit"])

        # The shutdown response must be fully swallowed: the client's
        # stdout must never receive a single byte during this whole
        # exchange. The background reader thread (Proxy._reader) is the
        # only thing draining proc.stdout, so check its queue instead of
        # reading the stream directly (which would race that thread).
        proxy._reader.join(timeout=5)
        forwarded = []
        while True:
            try:
                item = proxy.out_queue.get_nowait()
            except queue.Empty:
                break
            if item is not None:
                forwarded.append(item)
        self.assertEqual(forwarded, [], "client stdout must receive 0 bytes; shutdown response leaked")

    # (c) does NOT exit while a request is in flight.
    def test_c_no_exit_while_inflight(self):
        proxy = self.make_proxy(idle_seconds=1, fake_delay=2.5)
        mid, _raw_req = proxy.send_request("test/echo", {"n": 1})

        # idle_seconds has clearly elapsed by now, but the request is
        # still in flight: the proxy must still be alive.
        time.sleep(1.5)
        self.assertIsNone(proxy.proc.poll(), "proxy exited while a request was in flight")

        item = proxy.read_message(timeout=5)
        self.assertIsNot(item, TIMEOUT, "delayed response was never delivered")
        _raw_resp, parsed = item
        self.assertEqual(parsed.get("id"), mid)

        returncode = proxy.proc.wait(timeout=5)
        self.assertEqual(returncode, 0, "proxy should exit shortly after the in-flight request completes")

    # (d) activity resets the idle timer.
    def test_d_activity_resets_timer(self):
        proxy = self.make_proxy(idle_seconds=1)
        deadline = time.time() + 2.0
        while time.time() < deadline:
            mid, _raw = proxy.send_request("test/echo", {})
            item = proxy.read_message(timeout=2)
            self.assertIsNot(item, TIMEOUT)
            _raw_resp, parsed = item
            self.assertEqual(parsed.get("id"), mid)
            time.sleep(0.5)
        self.assertIsNone(proxy.proc.poll(), "proxy exited despite continuous activity")
        proxy.close()

    # (e) client EOF -> proxy exits, child gone.
    def test_e_client_eof_exits_and_child_gone(self):
        proxy = self.make_proxy(idle_seconds=60, pidfile=True)
        deadline = time.time() + 5
        while not os.path.exists(proxy.pidfile_path) and time.time() < deadline:
            time.sleep(0.05)
        self.assertTrue(os.path.exists(proxy.pidfile_path), "fake server never started")
        child_pid = int(Path(proxy.pidfile_path).read_text().strip())

        proxy.proc.stdin.close()
        returncode = proxy.proc.wait(timeout=10)
        self.assertEqual(returncode, 0)

        deadline = time.time() + 5
        alive = True
        while time.time() < deadline:
            try:
                os.kill(child_pid, 0)
            except OSError:
                alive = False
                break
            time.sleep(0.1)
        self.assertFalse(alive, "fake language-server child was still alive after proxy exit")

    # (f) disabled path execs directly; idle>0 routes through dart-lsp-idle.
    def _run_dart_lsp(self, idle_env, result_path):
        fvm_cache = self.tmp_path / ("fvm-%s" % (idle_env or "unset"))
        project_dir = self.tmp_path / ("project-%s" % (idle_env or "unset"))
        fake_home = self.tmp_path / ("home-%s" % (idle_env or "unset"))
        project_dir.mkdir(parents=True, exist_ok=True)
        fake_home.mkdir(parents=True, exist_ok=True)
        default_dart = fvm_cache / "default" / "bin" / "cache" / "dart-sdk" / "bin" / "dart"
        write_executable(default_dart, FAKE_DART_SRC)

        env = dict(os.environ)
        env["HOME"] = str(fake_home)
        env["FVM_CACHE_PATH"] = str(fvm_cache)
        env["FAKE_DART_RESULT"] = str(result_path)
        if idle_env is None:
            env.pop("DART_LSP_IDLE_SECONDS", None)
        else:
            env["DART_LSP_IDLE_SECONDS"] = idle_env

        proc = subprocess.Popen([str(DART_LSP_LAUNCHER)], cwd=str(project_dir), env=env,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        launcher_pid = proc.pid
        stdout, stderr = proc.communicate(timeout=15)
        self.assertEqual(proc.returncode, 0, "bin/dart-lsp failed: stderr=%r" % stderr)
        return launcher_pid

    def test_f_disabled_vs_enabled_routing(self):
        result_disabled = self.tmp_path / "result-disabled.txt"
        launcher_pid = self._run_dart_lsp("0", result_disabled)
        self.assertTrue(result_disabled.exists(), "fake dart never ran (disabled path)")
        data = dict(kv.split("=", 1) for kv in result_disabled.read_text().split())
        self.assertEqual(int(data["pid"]), launcher_pid,
                          "DART_LSP_IDLE_SECONDS=0 must exec the SDK directly (same pid as launcher)")

        result_enabled = self.tmp_path / "result-enabled.txt"
        launcher_pid2 = self._run_dart_lsp("5", result_enabled)
        self.assertTrue(result_enabled.exists(), "fake dart never ran (enabled path)")
        data2 = dict(kv.split("=", 1) for kv in result_enabled.read_text().split())
        self.assertNotEqual(int(data2["pid"]), launcher_pid2,
                             "idle>0 must run the SDK as a child of dart-lsp-idle, not exec it directly")
        self.assertEqual(int(data2["ppid"]), launcher_pid2,
                          "the idle wrapper keeps the launcher's pid (exec), so it is the fake dart's parent")

    # (g) server-originated notifications must NOT reset the idle clock:
    # a busy worktree (edits/git checkout/pub get/builds) keeps a real
    # analysis server emitting publishDiagnostics/$/analyzerStatus even
    # when the client itself has gone quiet, and that must still count
    # as idle.
    def test_g_server_notifications_do_not_reset_idle(self):
        proxy = self.make_proxy(idle_seconds=1, spam_interval=0.2)
        try:
            returncode = proxy.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.fail("proxy never exited: server notifications kept resetting the idle clock")
        elapsed = time.time() - proxy.start_time
        self.assertEqual(returncode, 0)
        self.assertLess(elapsed, 3.0,
                         "proxy should go idle ~1s after startup (no client traffic ever sent), "
                         "not be kept alive by the notification spam; took %.2fs" % elapsed)

    # (h) SIGTERM is forwarded to the child, then the proxy exits.
    def test_h_sigterm_forwards_and_child_gone(self):
        proxy = self.make_proxy(idle_seconds=60, pidfile=True)
        deadline = time.time() + 5
        while not os.path.exists(proxy.pidfile_path) and time.time() < deadline:
            time.sleep(0.05)
        self.assertTrue(os.path.exists(proxy.pidfile_path), "fake server never started")
        child_pid = int(Path(proxy.pidfile_path).read_text().strip())

        # Make sure the proxy is actually up and running (signal handlers
        # installed, pump threads alive) before sending a signal, or
        # delivery can race Python's own startup and hit the default
        # disposition instead of our handler, making the test flaky.
        mid, _raw = proxy.send_request("test/echo", {})
        item = proxy.read_message(timeout=5)
        self.assertIsNot(item, TIMEOUT, "proxy never became responsive before SIGTERM")
        _raw_resp, parsed = item
        self.assertEqual(parsed.get("id"), mid)

        proxy.proc.send_signal(signal.SIGTERM)
        returncode = proxy.proc.wait(timeout=5)
        self.assertEqual(returncode, 128 + signal.SIGTERM,
                          "proxy should exit with 128+signum after forwarding SIGTERM")

        deadline = time.time() + 5
        alive = True
        while time.time() < deadline:
            try:
                os.kill(child_pid, 0)
            except OSError:
                alive = False
                break
            time.sleep(0.1)
        self.assertFalse(alive, "fake language-server child was still alive after SIGTERM")

    # (i) framing robustness: byte-by-byte delivery, a lowercase header
    # name, an unrelated extra header, and two messages arriving in a
    # single write() call.
    def test_i_framing_robustness(self):
        proxy = self.make_proxy(idle_seconds=60)

        # Byte-by-byte writes + lowercase "content-length" + an extra
        # Content-Type header: the parser must not assume a message
        # arrives in one read() call, must be header-name-case-
        # insensitive, and must tolerate unrelated headers.
        mid1 = proxy.next_id()
        body1 = ('{"jsonrpc": "2.0", "id": %d, "method": "test/echo", "params": {}}' % mid1).encode()
        raw1 = (
            ("content-length: %d\r\n" % len(body1)) +
            "Content-Type: application/vscode-jsonrpc; charset=utf-8\r\n" +
            "\r\n"
        ).encode("ascii") + body1
        for i in range(len(raw1)):
            proxy.proc.stdin.write(raw1[i:i + 1])
            proxy.proc.stdin.flush()

        item = proxy.read_message(timeout=5)
        self.assertIsNot(item, TIMEOUT, "byte-by-byte / lowercase-header message was never answered")
        _raw, parsed = item
        self.assertEqual(parsed.get("id"), mid1)

        # Two complete messages delivered in a single write() call: the
        # parser must stop exactly at each message's boundary, not
        # overrun into the next one.
        mid2 = proxy.next_id()
        mid3 = proxy.next_id()
        raw2 = build_raw_request(mid2, "test/echo", {"n": 2})
        raw3 = build_raw_request(mid3, "test/echo", {"n": 3})
        proxy.proc.stdin.write(raw2 + raw3)
        proxy.proc.stdin.flush()

        seen = {}
        for _ in range(2):
            item = proxy.read_message(timeout=5)
            self.assertIsNot(item, TIMEOUT, "batched message was never answered")
            _raw, parsed = item
            seen[parsed.get("id")] = parsed
        self.assertIn(mid2, seen)
        self.assertIn(mid3, seen)

        proxy.close()

    # (j, fix 1) a client request that arrives right as idle-shutdown
    # starts must be dropped, not forwarded into a child whose stdin is
    # about to close -- and the proxy must still exit cleanly, with no
    # unhandled exception on stderr.
    def test_j_late_request_during_closing_is_dropped_cleanly(self):
        proxy = self.make_proxy(idle_seconds=1, shutdown_delay=3)

        # Let it go idle naturally (no client traffic): idle_seconds
        # elapses, handle_idle_shutdown starts and sets state.closing,
        # sends "shutdown" -- but the fake server sleeps 3s before
        # acking it, which holds this window open long enough to land a
        # request inside it deterministically.
        time.sleep(1.3)
        self.assertIsNone(proxy.proc.poll(), "proxy exited before the shutdown-ack window even opened")

        proxy.send_request("test/echo", {"late": True})

        try:
            returncode = proxy.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.fail("proxy never exited after a late request arrived during the closing window")
        self.assertEqual(returncode, 0)

        stderr = proxy.proc.stderr.read().decode(errors="replace")
        self.assertNotIn("Traceback", stderr, "pump crashed instead of dropping the late request cleanly")

        entries = parse_log(proxy.log_path)
        recv_methods = [raw_method(raw) for tag, raw in entries if tag == "RECV"]
        self.assertNotIn("test/echo", recv_methods, "late request must never reach the child once closing")
        self.assertEqual(recv_methods, ["shutdown", "exit"])

    # (k, fix 3) malformed framing on the client stream must not crash a
    # pump thread silently -- it should log one line and end that pump,
    # which tears the whole proxy down cleanly via client-EOF handling.
    def test_k_malformed_content_length_ends_stream_cleanly(self):
        proxy = self.make_proxy(idle_seconds=60)
        proxy.proc.stdin.write(b"Content-Length: notanumber\r\n\r\n")
        proxy.proc.stdin.flush()

        try:
            returncode = proxy.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.fail("proxy never tore down after malformed framing on the client stream")
        self.assertEqual(returncode, 0)

        stderr = proxy.proc.stderr.read().decode(errors="replace")
        self.assertNotIn("Traceback", stderr, "malformed framing must not raise an unhandled exception")

    # (l, fix 6) a python3 that exists on PATH but doesn't actually run
    # (e.g. an Xcode CLT stub or an unconfigured pyenv shim) must not be
    # trusted just because `command -v` finds it.
    def test_l_broken_python3_falls_back_to_direct_exec(self):
        fvm_cache = self.tmp_path / "fvm-brokenpy"
        project_dir = self.tmp_path / "project-brokenpy"
        fake_home = self.tmp_path / "home-brokenpy"
        broken_bin = self.tmp_path / "broken-python3-bin"
        project_dir.mkdir(parents=True, exist_ok=True)
        fake_home.mkdir(parents=True, exist_ok=True)
        broken_bin.mkdir(parents=True, exist_ok=True)
        default_dart = fvm_cache / "default" / "bin" / "cache" / "dart-sdk" / "bin" / "dart"
        write_executable(default_dart, FAKE_DART_SRC)
        write_executable(broken_bin / "python3", "#!/bin/sh\nexit 1\n")

        result_path = self.tmp_path / "result-brokenpy.txt"
        env = dict(os.environ)
        env["HOME"] = str(fake_home)
        env["FVM_CACHE_PATH"] = str(fvm_cache)
        env["FAKE_DART_RESULT"] = str(result_path)
        env["DART_LSP_IDLE_SECONDS"] = "5"
        # Shadow any real python3 later in PATH with the always-fails one.
        env["PATH"] = str(broken_bin) + os.pathsep + env.get("PATH", "")

        proc = subprocess.Popen([str(DART_LSP_LAUNCHER)], cwd=str(project_dir), env=env,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        launcher_pid = proc.pid
        stdout, stderr = proc.communicate(timeout=15)
        self.assertEqual(proc.returncode, 0, "bin/dart-lsp failed: stderr=%r" % stderr)
        self.assertTrue(result_path.exists(), "fake dart never ran")
        data = dict(kv.split("=", 1) for kv in result_path.read_text().split())
        self.assertEqual(int(data["pid"]), launcher_pid,
                          "a broken python3 on PATH must fall back to exec'ing the SDK directly")
        self.assertIn(b"no usable python3", stderr)


if __name__ == "__main__":
    unittest.main()
