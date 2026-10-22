#!/usr/bin/env python3
# SPDX-License-Identifier: 0BSD
"""Select full CI validation, conservatively handling unavailable history."""
import os
import re
import subprocess
import sys


def needs_full_validation(event, base):
    # Missing comparison history must never turn into skipped package checks.
    if event not in ('push', 'pull_request'):
        return True
    if not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', base) or set(base) == {'0'}:
        return True
    available = subprocess.run(
        ['git', 'cat-file', '-e', base + '^{commit}'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    if available.returncode:
        print('Comparison commit unavailable; running full package validation.', file=sys.stderr)
        return True
    # Preserve failures unrelated to unavailable history. NUL delimiters also
    # preserve paths containing newlines and avoid Git's quoted-path output.
    paths = subprocess.check_output(
        ['git', 'diff', '--no-renames', '--name-only', '-z', base, 'HEAD', '--'],
    ).split(b'\0')
    return any(
        path in (b'PKGBUILD', b'.SRCINFO', b'upstream.py',
                 b'upstream-depends.txt', b'upstream-layout.json')
        or path.startswith((b'scripts/', b'tests/', b'.github/workflows/'))
        for path in paths
    )


if __name__ == '__main__':
    # A broken checkout is an error, rather than an unavailable-history fallback.
    subprocess.check_call(['git', 'rev-parse', '--verify', 'HEAD^{commit}'],
                          stdout=subprocess.DEVNULL)
    print('full=' + str(needs_full_validation(
        os.environ['EVENT'], os.environ.get('BASE_SHA', ''),
    )).lower())
