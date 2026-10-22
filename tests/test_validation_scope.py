# SPDX-License-Identifier: 0BSD
"""Exercise CI scope detection against real Git histories, without networking."""
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / 'scripts/validation-scope.py'
spec = importlib.util.spec_from_file_location('scope', SCRIPT)
scope = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scope)


class ValidationScopeTests(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.repo = Path(work.name) / 'repo'
        self.repo.mkdir()
        self.git('init', '-q')
        self.git('config', 'user.name', 'Scope test')
        self.git('config', 'user.email', 'scope@example.invalid')
        self.commit('README.md')
        self.base = self.git('rev-parse', 'HEAD').strip()

    def git(self, *args):
        return subprocess.check_output(['git', *args], cwd=self.repo, text=True)

    def commit(self, name):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('changed\n')
        self.git('add', '--', name)
        self.git('commit', '-qm', 'Fixture change')

    def run_scope(self, event='push', base=None, repo=None):
        result = subprocess.run(
            ['python3', str(SCRIPT)], cwd=repo or self.repo,
            env={**os.environ, 'EVENT': event,
                 'BASE_SHA': self.base if base is None else base},
            text=True, capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(result.stdout, ('full=true\n', 'full=false\n'))
        return result.stdout == 'full=true\n'

    def test_documentation_only_and_unchanged(self):
        self.assertFalse(self.run_scope())
        self.commit('docs/guide.md')
        self.assertFalse(self.run_scope())
        self.assertFalse(self.run_scope(event='pull_request'))

    def test_all_package_sensitive_paths(self):
        for name in ('PKGBUILD', '.SRCINFO', 'upstream.py', 'upstream-depends.txt',
                     'upstream-layout.json', 'scripts/helper.sh', 'tests/check.py',
                     '.github/workflows/validate.yml', 'scripts/line\nbreak.py'):
            with self.subTest(name=name):
                self.base = self.git('rev-parse', 'HEAD').strip()
                self.commit(name)
                self.assertTrue(self.run_scope())
                self.assertTrue(self.run_scope(event='pull_request'))

    def test_removed_package_file(self):
        self.commit('PKGBUILD')
        self.base = self.git('rev-parse', 'HEAD').strip()
        self.git('rm', '-q', 'PKGBUILD')
        self.git('commit', '-qm', 'Remove package fixture')
        self.assertTrue(self.run_scope())

    def test_reported_missing_before_commit(self):
        self.assertTrue(self.run_scope(base='f643a96e296df1eac8f50e574444104654882e0a'))

    def test_rename_out_of_package_scope(self):
        self.commit('scripts/helper.py')
        self.base = self.git('rev-parse', 'HEAD').strip()
        (self.repo / 'docs').mkdir()
        self.git('mv', 'scripts/helper.py', 'docs/retired-helper.py')
        self.git('commit', '-qm', 'Move helper out of package scope')
        self.assertTrue(self.run_scope())

    def test_force_push_previous_tip_not_in_fetched_history(self):
        self.commit('scripts/old-tip.py')
        old_tip = self.git('rev-parse', 'HEAD').strip()
        self.git('reset', '--hard', self.base)
        self.commit('docs/replacement.md')
        clone = self.repo.parent / 'clone'
        subprocess.check_call(['git', 'clone', '-q', '--no-local', str(self.repo), str(clone)])
        self.assertTrue(self.run_scope(base=old_tip, repo=clone))

    def test_shallow_clone_missing_base(self):
        self.commit('docs/latest.md')
        clone = self.repo.parent / 'shallow'
        subprocess.check_call(['git', 'clone', '-q', '--depth=1', self.repo.as_uri(), str(clone)])
        self.assertTrue(self.run_scope(repo=clone))

    def test_missing_zero_and_invalid_base(self):
        for base in ('', '0' * 40, '--stat', 'invalid', 'a' * 64):
            with self.subTest(base=base):
                self.assertTrue(self.run_scope(base=base))

    def test_manual_and_unknown_events(self):
        self.assertTrue(self.run_scope(event='workflow_dispatch'))
        self.assertTrue(self.run_scope(event='unknown'))

    def test_diff_failure_is_not_suppressed(self):
        with patch.object(scope.subprocess, 'run') as available, \
             patch.object(scope.subprocess, 'check_output',
                          side_effect=subprocess.CalledProcessError(128, 'git diff')):
            available.return_value.returncode = 0
            with self.assertRaises(subprocess.CalledProcessError):
                scope.needs_full_validation('push', self.base)

    def test_broken_checkout_is_an_error(self):
        result = subprocess.run(['python3', str(SCRIPT)], cwd=self.repo.parent,
                                env={**os.environ, 'EVENT': 'workflow_dispatch'},
                                text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('full=', result.stdout)
