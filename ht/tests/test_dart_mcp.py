#!/usr/bin/env python3
"""Hermetic launcher tests; no real pub cache or network is used."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

LAUNCHER = Path(__file__).resolve().parents[2] / 'bin' / 'dart-mcp'


class DartMcpTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='dart mcp ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.dart = self.root / 'fvm path/default/bin/cache/dart-sdk/bin/dart'
        self.dart.parent.mkdir(parents=True)
        self.dart.write_text('''#!/usr/bin/env python3
import json, os, sys
with open(os.environ['RECORD'], 'a') as f:
    f.write(json.dumps([sys.argv[1:], os.environ.get('PUB_CACHE')]) + '\\n')
args = sys.argv[1:]
if args == ['--version']:
    print('Dart SDK version: ' + os.environ.get('VERSION', '3.12.2') + ' (stable)', file=sys.stderr)
    sys.exit(int(os.environ.get('VERSION_EXIT', '0')))
elif args == ['pub', 'global', 'list']:
    print(os.environ.get('INSTALLED', ''))
elif args[:3] == ['pub', 'global', 'activate']:
    print('activation output')
    sys.exit(int(os.environ.get('ACTIVATE_EXIT', '0')))
elif args[:3] == ['pub', 'global', 'run']:
    print('server output')
''')
        self.dart.chmod(0o755)
        self.record = self.root / 'calls'
        self.env = dict(os.environ, HOME=str(self.root), FVM_CACHE_PATH=str(self.root / 'fvm path'),
                        XDG_CACHE_HOME=str(self.root / 'cache path'), PUB_CACHE=str(self.root / 'user-cache'),
                        RECORD=str(self.record))

    def run_launcher(self, *args, **env):
        return subprocess.run([str(LAUNCHER), *args], cwd=self.root,
                              env=dict(self.env, **env), capture_output=True, text=True)

    def calls(self):
        return [json.loads(line) for line in self.record.read_text().splitlines()]

    def test_activate_then_forward_arguments_and_isolate_cache(self):
        result = self.run_launcher('--disable', 'analysis', 'arg with spaces')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, 'server output\n')
        self.assertIn('activation output', result.stderr)
        calls = self.calls()
        self.assertEqual([c[0] for c in calls], [['--version'], ['pub', 'global', 'list'],
            ['pub', 'global', 'activate', 'dart_mcp_server', '1.2.2'],
            ['pub', 'global', 'run', 'dart_mcp_server:dart_mcp_server', '--disable', 'analysis', 'arg with spaces']])
        self.assertTrue(all(c[1] == str(self.root / 'cache path/dart-flutter-ht/pub-cache') for c in calls))

    def test_only_exact_version_skips_activation(self):
        result = self.run_launcher(INSTALLED='other 1.2.2\ndart_mcp_server 1.2.2')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any(c[0][2:3] == ['activate'] for c in self.calls()))
        self.record.unlink()
        result = self.run_launcher(INSTALLED='dart_mcp_server 1.2.20')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(any(c[0][2:3] == ['activate'] for c in self.calls()))

    def test_activation_failure_stops(self):
        result = self.run_launcher(ACTIVATE_EXIT='7')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, '')
        self.assertIn('activation output', result.stderr)
        self.assertFalse(any(c[0][2:3] == ['run'] for c in self.calls()))

    def test_old_sdk_fails_without_activation(self):
        result = self.run_launcher(VERSION='3.11.9')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('3.12', result.stderr)
        self.assertEqual([c[0] for c in self.calls()], [['--version']])

    def test_unparseable_version_fails_closed(self):
        result = self.run_launcher(VERSION='unknown')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('unable to parse', result.stderr)
        self.assertEqual([c[0] for c in self.calls()], [['--version']])

    def test_sdk_version_failure_stops(self):
        result = self.run_launcher(VERSION_EXIT='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('unable to determine', result.stderr)
        self.assertEqual([c[0] for c in self.calls()], [['--version']])

    def test_manifests_use_launcher_with_client_analysis_policy(self):
        root = LAUNCHER.parents[1]
        claude = json.loads((root / '.claude-plugin/plugin.json').read_text())
        codex = json.loads((root / '.codex-plugin/plugin.json').read_text())
        marketplace = json.loads((root / '.agents/plugins/marketplace.json').read_text())
        self.assertEqual(marketplace['name'], codex['name'])
        self.assertEqual([plugin['name'] for plugin in marketplace['plugins']], [codex['name']])
        self.assertEqual((claude['name'], claude['version']), ('dart-flutter-ht', '1.0.7-ht.1'))
        self.assertEqual((codex['name'], codex['version']), ('dart-flutter-ht', '1.0.7-ht.1'))
        claude_server = claude['mcpServers']['dart-mcp-server']
        codex_server = codex['mcpServers']['dart-mcp-server']
        self.assertEqual(claude_server['command'], '${CLAUDE_PLUGIN_ROOT}/bin/dart-mcp')
        self.assertEqual(claude_server['args'], ['--disable', 'analysis'])
        self.assertEqual(codex_server['command'], './bin/dart-mcp')
        self.assertEqual(codex_server['cwd'], '.')
        self.assertEqual(codex_server['args'], [])

    def test_project_sdk_selected(self):
        project = self.root / 'project'
        project.mkdir()
        (project / '.fvmrc').write_text('{"flutter": "pinned"}')
        pinned = self.root / 'fvm path/versions/pinned/bin/cache/dart-sdk/bin/dart'
        pinned.parent.mkdir(parents=True)
        pinned.write_text(self.dart.read_text())
        pinned.chmod(0o755)
        self.dart.unlink()
        result = subprocess.run([str(LAUNCHER)], cwd=project, env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
