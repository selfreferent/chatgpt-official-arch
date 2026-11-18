#!/usr/bin/env python3
# SPDX-License-Identifier: 0BSD
"""Strict upstream metadata, update, and archive validation; standard library only."""
import argparse
import contextlib
import configparser
import difflib
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parent
INDEX = 'https://persistent.oaistatic.com/codex-app-prod/linux/deb/dists/stable/main/binary-amd64/Packages'
VERSION = re.compile(r'[0-9]+(?:\.[0-9]+)+\Z')
SHA = re.compile(r'[0-9a-f]{64}\Z')

class Invalid(ValueError):
    pass

class ReviewRequired(Invalid):
    """Well-formed declarations which require a maintainer's policy decision."""
    def __init__(self, findings):
        self.findings = findings
        super().__init__(json.dumps(findings, sort_keys=True))

def require(ok, message):
    if not ok:
        raise Invalid(message)

def version_key(value):
    require(bool(VERSION.fullmatch(value)), f'invalid version: {value!r}')
    parts = tuple(int(p) for p in value.split('.'))
    while parts and parts[-1] == 0:
        parts = parts[:-1]
    return parts

def control(text):
    records, fields, last = [], {}, None
    for line in text.splitlines() + ['']:
        if not line:
            if fields:
                records.append(fields)
            fields, last = {}, None
        elif line[0].isspace():
            require(last is not None, 'orphan control continuation')
            fields[last] += ' ' + line.strip()
        else:
            match = re.fullmatch(r'([A-Za-z0-9][A-Za-z0-9-]*):[ \t]*(.*)', line)
            require(match is not None, f'malformed control line: {line!r}')
            key, value = match.groups()
            key = key.lower()
            require(key not in fields, f'duplicate control field: {key}')
            require(not any(ord(c) < 32 for c in value), 'control characters in field')
            fields[key], last = value, key
    return records

def normalize_depends(value):
    require(bool(value.strip()), 'missing Depends')
    # Comma-separated clauses are a conjunction: their order and exact
    # duplicates do not change requirements. Preserve alternative order and
    # every version constraint so provider/requirement changes still need review.
    groups = []
    for group in value.split(','):
        alternatives = []
        for alt in group.split('|'):
            match = re.fullmatch(r'\s*([a-z0-9][a-z0-9+.-]*(?::[a-z0-9-]+)?)(?:\s*\(\s*(<<|<=|=|>=|>>)\s*([^\s()]+)\s*\))?\s*', alt)
            require(match is not None, f'unsupported/malformed dependency: {alt!r}')
            name, op, ver = match.groups()
            alternatives.append(name + (f' ({op} {ver})' if op else ''))
        groups.append(' | '.join(alternatives))
    return ', '.join(sorted(set(groups)))

def check_depends(actual, expected):
    actual, expected = normalize_depends(actual), normalize_depends(expected)
    require(actual == expected, 'upstream dependency drift; review Arch mapping:\n' + ''.join(
        difflib.unified_diff([expected+'\n'], [actual+'\n'], fromfile='reviewed Depends', tofile='upstream Depends')))
    return actual

def dependency_changes(actual, expected):
    """Accept only strictly lowered numeric minima on unique single providers."""
    actual, expected = normalize_depends(actual), normalize_depends(expected)
    old, new = set(expected.split(', ')), set(actual.split(', '))
    removed, added = sorted(old-new), sorted(new-old)
    if not removed and not added:
        return [], True
    changes = [{'kind':'dependencies', 'removed':removed, 'added':added}]
    def providers(clause):
        return [alt.split(' ', 1)[0].split(':', 1)[0] for alt in clause.split(' | ')]
    old_names = [name for clause in old for name in providers(clause)]
    new_names = [name for clause in new for name in providers(clause)]
    if sorted(old_names) != sorted(new_names) or len(removed) != len(added):
        return changes, False
    remaining = set(added)
    for clause in removed:
        match = re.fullmatch(r'([a-z0-9][a-z0-9+.-]*) \(>= ([0-9]+(?:\.[0-9]+)+)\)', clause)
        if not match:
            return changes, False
        name, threshold = match.groups()
        if old_names.count(name) != 1 or new_names.count(name) != 1:
            return changes, False
        matches = [c for c in remaining if c.startswith(name+' (>= ')]
        if len(matches) != 1:
            return changes, False
        lowered = re.fullmatch(re.escape(name)+r' \(>= ([0-9]+(?:\.[0-9]+)+)\)', matches[0])
        if not lowered or version_key(lowered[1]) >= version_key(threshold):
            return changes, False
        remaining.remove(matches[0])
    return changes, not remaining

def select(text):
    found, numeric, signatures = {}, {}, {}
    for fields in control(text):
        if fields.get('package') != 'chatgpt' or fields.get('architecture') != 'amd64':
            continue
        for key in ('version', 'filename', 'sha256', 'depends'):
            require(bool(fields.get(key)), f'missing {key} in chatgpt/amd64 stanza')
        ver = fields['version']
        key = version_key(ver)
        require(SHA.fullmatch(fields['sha256']), f'invalid SHA256 for {ver}')
        require(fields['filename'] == f'pool/main/c/chatgpt/chatgpt_{ver}_amd64.deb', f'unexpected Filename for {ver}')
        candidate = {k: fields[k] for k in ('package', 'version', 'architecture', 'filename', 'sha256')}
        candidate['depends'] = normalize_depends(fields['depends'])
        signature = dict(fields, depends=candidate['depends'])
        require(ver not in signatures or signatures[ver] == signature, f'conflicting duplicate version: {ver}')
        signatures[ver] = signature
        require(key not in numeric or numeric[key] == ver, f'ambiguous equivalent version: {ver}')
        found[ver], numeric[key] = candidate, ver
    require(bool(found), 'no chatgpt/amd64 package')
    return found[numeric[max(numeric)]]

def assignments(text):
    patterns = {
        'version': r'^pkgver=([^\n]+)$',
        'release': r'^pkgrel=([0-9]+)$',
        'sha256': r"^sha256sums_x86_64=\(\n[ \t]*'([0-9a-f]{64})'[ \t]*\n\)$",
    }
    result = {}
    for key, pattern in patterns.items():
        matches = list(re.finditer(pattern, text, re.M))
        require(len(matches) == 1, f'expected exactly one canonical {key} assignment')
        result[key] = matches[0]
    version_key(result['version'][1])
    require(int(result['release'][1]) > 0, 'invalid pkgrel')
    return result

def validate_local_sources(pkgbuild):
    path=Path(pkgbuild)
    text=path.read_text()
    expected=['upstream.py','upstream-depends.txt','upstream-layout.json']
    sources=re.findall(r"^source=\(\n((?:[ \t]*'[^'\n]+'[ \t]*\n)+)\)",text,re.M)
    sums=re.findall(r"^sha256sums=\(\n((?:[ \t]*'[0-9a-f]{64}'[ \t]*\n)+)\)",text,re.M)
    require(len(sources)==1 and len(sums)==1,'noncanonical local source/checksum arrays')
    names=re.findall(r"'([^']+)'",sources[0]); hashes=re.findall(r"'([^']+)'",sums[0])
    require(names==expected and len(hashes)==len(expected),'unexpected local source inventory')
    for name,digest in zip(names,hashes):
        require(hashlib.sha256((path.parent/name).read_bytes()).hexdigest()==digest,
                f'local source checksum mismatch: {name}; review edits and refresh checksums')

def discover(pkgbuild, index_file=None, depends_file=None, workflow=False):
    if index_file:
        text = Path(index_file).read_text()
    else:
        text = subprocess.check_output(['curl', '--fail', '--silent', '--show-error', '--location',
            '--proto', '=https', '--proto-redir', '=https', '--tlsv1.2', '--max-time', '120', INDEX], text=True)
    upstream = select(text)
    expected = Path(depends_file or ROOT/'upstream-depends.txt').read_text()
    changes, compatible = dependency_changes(upstream['depends'], expected)
    if not workflow:
        check_depends(upstream['depends'], expected)
    local = assignments(Path(pkgbuild).read_text())
    ver, sha = local['version'][1], local['sha256'][1]
    require(version_key(upstream['version']) >= version_key(ver), 'refusing upstream downgrade')
    require(version_key(upstream['version']) != version_key(ver) or upstream['version'] == ver,
            'ambiguous upstream/local equivalent versions')
    status = 'current' if ver == upstream['version'] and sha == upstream['sha256'] else 'outdated'
    result = dict(upstream, status=status, packaged_version=ver, packaged_sha256=sha,
                  checksum_replaced=ver == upstream['version'] and sha != upstream['sha256'])
    if workflow:
        result.update(outcome=('requires_review' if not compatible else
                               'candidate' if status=='outdated' or changes else 'no_change'),
                      dependency_changes=changes, findings=changes if not compatible else [],
                      validation='pending' if compatible else 'blocked')
    return result

def update(pkgbuild, upstream):
    path = Path(pkgbuild)
    text = path.read_text()
    fields = assignments(text)
    changes = upstream.get('dependency_changes', [])
    if upstream.get('outcome') == 'requires_review':
        return False
    if upstream['status'] == 'current' and not changes:
        return False
    release = int(fields['release'][1]) + 1 if upstream['version']==fields['version'][1] else 1
    replacements = {'version': upstream['version'], 'release': str(release), 'sha256': upstream['sha256']}
    changed = text
    for key, match in sorted(fields.items(), key=lambda item: item[1].start(1), reverse=True):
        changed = changed[:match.start(1)] + replacements[key] + changed[match.end(1):]
    baseline = path.parent/'upstream-depends.txt'
    if changes:
        findings, compatible = dependency_changes(upstream['depends'], baseline.read_text())
        require(compatible and findings == changes, 'dependency relaxation was not independently reproduced')
        old_hash = hashlib.sha256(baseline.read_bytes()).hexdigest()
        new_baseline = normalize_depends(upstream['depends'])+'\n'
        new_hash = hashlib.sha256(new_baseline.encode()).hexdigest()
        require(changed.count("'"+old_hash+"'")==1, 'missing unique dependency baseline checksum')
        changed = changed.replace("'"+old_hash+"'", "'"+new_hash+"'")
    # Generate from a temporary recipe first; failures leave the working pair untouched.
    with tempfile.TemporaryDirectory(dir=path.parent, prefix='.updater-') as work:
        staged = Path(work)/'PKGBUILD'
        staged.write_text(changed)
        srcinfo = subprocess.check_output(['makepkg', '--printsrcinfo'],
                                         cwd=work, text=True)
        require(bool(srcinfo.strip()), 'empty generated .SRCINFO')
    path.write_text(changed)
    (path.parent/'.SRCINFO').write_text(srcinfo)
    if changes:
        baseline.write_text(new_baseline)
    return True

def safe_name(name):
    require(not name.startswith('/'), f'absolute archive path: {name}')
    while name.startswith('./'):
        name = name[2:]
    name = name.rstrip('/')
    require('..' not in name.split('/'), f'archive traversal: {name}')
    require(not any(ord(c) < 32 for c in name), 'control character in archive path')
    return str(PurePosixPath(name))

def resolved_link(name, target):
    require(not target.startswith('/'), f'absolute symlink: {name} -> {target}')
    parts = list(PurePosixPath(name).parent.parts)
    for part in target.split('/'):
        if part in ('', '.'):
            continue
        if part == '..':
            require(bool(parts), f'escaping symlink: {name} -> {target}')
            parts.pop()
        else:
            parts.append(part)
    return '/'.join(parts)

def inspect_tar(archive, package=False):
    members, links = {}, {}
    for member in archive:
        name = safe_name(member.name)
        require(name not in members, f'duplicate archive destination: {name}')
        require(member.isdir() or member.isfile() or member.issym(), f'unsupported file type: {name}')
        if not member.issym():
            require(not member.mode & 0o6002, f'unsafe archive mode: {name}')
        require(member.uid == 0 and member.gid == 0, f'non-root ownership: {name}')
        if member.issym():
            links[name] = resolved_link(name, member.linkname)
        members[name] = member
    for name in members:
        ancestors = PurePosixPath(name).parents
        require(not any(str(p) in members and not members[str(p)].isdir() for p in ancestors),
                f'archive extraction through non-directory: {name}')
    for name, target in links.items():
        require(not any(str(p) in links for p in PurePosixPath(target).parents),
                f'symlink target passes through another symlink: {name}')
        seen = {name}
        while target in links:
            require(target not in seen, f'symlink cycle: {name}')
            seen.add(target)
            target = links[target]
        require(target in members, f'dangling symlink: {name}')
        if name.startswith('usr/lib/chatgpt/'):
            require(target.startswith('usr/lib/chatgpt/'), f'application symlink escapes tree: {name}')
    return members

def read_member(archive, member):
    require(member.isfile() and member.size <= 2*1024*1024, f'invalid metadata file: {member.name}')
    return archive.extractfile(member).read().decode('utf-8')

def payload_checks(archive, members, layout, package=False):
    findings = []
    prefix = 'usr/lib/chatgpt/'
    for path in layout['required_app_files']:
        name = prefix + path
        require(name in members and members[name].isfile(), f'missing application file: {name}')
    for name in (prefix+'ChatGPT', prefix+'codex-launcher'):
        require(members[name].mode & 0o111, f'non-executable launcher: {name}')
    launcher = members.get('usr/bin/chatgpt')
    require(launcher is not None and launcher.issym(), 'missing/invalid launcher symlink')
    if launcher.linkname != '../lib/chatgpt/codex-launcher':
        findings.append({'kind':'launcher', 'expected':'../lib/chatgpt/codex-launcher', 'actual':launcher.linkname})
    desktop = members.get('usr/share/applications/chatgpt.desktop')
    require(desktop is not None, 'missing desktop entry')
    declarations=configparser.ConfigParser(interpolation=None)
    declarations.optionxform=str
    try:
        declarations.read_string(read_member(archive, desktop))
    except configparser.Error as exc:
        raise Invalid(f'malformed desktop entry: {exc}') from exc
    require('Desktop Entry' in declarations, 'missing Desktop Entry section')
    require(not declarations.defaults(), 'unexpected desktop defaults')
    values=dict(declarations['Desktop Entry'])
    for key in sorted(set(values) | set(layout['desktop'])):
        expected=layout['desktop'].get(key)
        if values.get(key) != expected:
            findings.append({'kind':'desktop', 'field':key, 'expected':expected, 'actual':values.get(key)})
    for section in declarations.sections():
        if section != 'Desktop Entry':
            findings.append({'kind':'desktop_section', 'section':section, 'actual':dict(declarations[section])})
    external = {name: 'symlink' if m.issym() else 'file' for name, m in members.items()
                if not m.isdir() and not name.startswith(prefix)}
    if package:
        expected = {'usr/bin/chatgpt':'symlink', 'usr/share/applications/chatgpt.desktop':'file',
                    'usr/share/pixmaps/chatgpt.png':'file',
                    'usr/share/doc/chatgpt-official-bin/third-party-notices':'file'}
        for name in ('.PKGINFO', '.BUILDINFO', '.MTREE'):
            require(name in external, f'missing package metadata: {name}')
            external.pop(name)
        allowed_dirs = {'usr','usr/bin','usr/lib','usr/lib/chatgpt','usr/share',
                        'usr/share/applications','usr/share/pixmaps','usr/share/doc',
                        'usr/share/doc/chatgpt-official-bin','.'}
        require(all(not m.isdir() or name in allowed_dirs or name.startswith(prefix)
                    for name,m in members.items()), 'unexpected packaged directory')
        notices = 'usr/share/doc/chatgpt-official-bin/third-party-notices'
    else:
        expected = layout['external']
        notices = 'usr/share/doc/chatgpt/copyright'
        allowed_dirs = {'.','usr/lib','usr/lib/chatgpt'}
        for name in expected:
            allowed_dirs.update(str(p) for p in PurePosixPath(name).parents)
        directories = sorted(name for name,m in members.items() if m.isdir() and name not in allowed_dirs and not name.startswith(prefix))
        if directories:
            findings.append({'kind':'integration_directories', 'added':directories})
    for name in ('usr/share/pixmaps/chatgpt.png', notices):
        require(name in members and members[name].isfile(), f'missing required integration file: {name}')
    if external != expected:
        findings.append({'kind':'integration_layout', 'expected':expected, 'actual':external})
    require('Electron contributors' in read_member(archive, members[notices]), 'third-party notices changed/missing')
    if findings:
        raise ReviewRequired(findings)

def compare_application(archive, members, tree):
    prefix='usr/lib/chatgpt/'
    tree=Path(tree)
    upstream=tree/'usr/lib/chatgpt'
    require(upstream.is_dir(), 'missing comparison application tree')
    expected={str(p.relative_to(tree)) for p in upstream.rglob('*')}
    actual={name for name in members if name.startswith(prefix)}
    require(expected==actual, 'packaged application inventory differs from upstream')
    for name in actual:
        member=members[name]; path=tree/name
        if member.isfile():
            with path.open('rb') as original, archive.extractfile(member) as packaged:
                require(hashlib.file_digest(original,'sha256').digest()==hashlib.file_digest(packaged,'sha256').digest(),
                        f'application bytes changed: {name}')
        elif member.issym():
            require(path.is_symlink() and os.readlink(path)==member.linkname, f'application symlink changed: {name}')
    print('packaged application matches upstream byte-for-byte')

@contextlib.contextmanager
def open_tar(path):
    # zstd support independent of the Python version on the builder.
    if str(path).endswith(('.zst', '.zstd')):
        with tempfile.TemporaryFile() as raw:
            subprocess.run(['zstd','-q','-d','-c','--',str(path)], stdout=raw, check=True)
            raw.seek(0)
            with tarfile.open(fileobj=raw, mode='r:') as archive:
                yield archive
    else:
        with tarfile.open(path, mode='r:*') as archive:
            yield archive

def unpack_deb(deb, work):
    # Read ar headers ourselves; never extract unchecked outer paths.
    names = set()
    with open(deb,'rb') as stream:
        require(stream.read(8) == b'!<arch>\n', 'invalid Debian ar archive')
        while header := stream.read(60):
            require(len(header)==60 and header[58:]==b'`\n', 'invalid ar header')
            name = header[:16].decode('ascii').strip().rstrip('/')
            require(name in {'debian-binary','_gpgorigin'} or
                    re.fullmatch(r'(control|data)\.tar\.(xz|gz|bz2|zst)', name), f'unexpected Debian ar member: {name}')
            require(name not in names, f'duplicate Debian ar member: {name}')
            names.add(name)
            size = int(header[48:58])
            require(size >= 0, 'negative ar size')
            with open(Path(work)/name,'wb') as output:
                remaining = size
                while remaining:
                    chunk = stream.read(min(remaining, 1024*1024))
                    require(bool(chunk), 'truncated ar member')
                    output.write(chunk)
                    remaining -= len(chunk)
            if size % 2:
                require(stream.read(1)==b'\n', 'invalid ar padding')
    require((Path(work)/'debian-binary').read_bytes()==b'2.0\n', 'unsupported Debian format')
    result=[]
    for kind in ('control','data'):
        matches=[Path(work)/n for n in names if n.startswith(kind+'.tar.')]
        require(len(matches)==1, f'expected exactly one {kind} archive')
        result.append(matches[0])
    return result

def validate_deb(deb, version, depends, layout, destination=None):
    with tempfile.TemporaryDirectory(prefix='chatgpt-deb-') as work:
        control_path, data_path = unpack_deb(deb, work)
        with open_tar(control_path) as archive:
            members = inspect_tar(archive)
            require('control' in members, 'missing Debian control')
            records = control(read_member(archive, members['control']))
            require(len(records)==1, 'expected one Debian control stanza')
            fields=records[0]
            require(fields.get('package')=='chatgpt' and fields.get('architecture')=='amd64', 'wrong Debian package/architecture')
            require(fields.get('version')==version, 'Debian version does not match PKGBUILD')
            check_depends(fields.get('depends',''), Path(depends).read_text())
        with open_tar(data_path) as archive:
            members=inspect_tar(archive)
            payload_checks(archive, members, json.loads(Path(layout).read_text()))
            if destination is None:
                return
            dest=Path(destination)
            require(not dest.exists(), 'extraction destination must not exist')
            dest.mkdir(parents=True)
            # No unchecked member reaches extraction. No ownership is imported.
            archive.extractall(dest, members=list(members.values()), filter='data')

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    local=sub.add_parser('local-sources')
    local.add_argument('--pkgbuild',default=str(ROOT/'PKGBUILD'))
    for name in ('check','update'):
        cmd=sub.add_parser(name)
        cmd.add_argument('--pkgbuild',default=os.environ.get('PKGBUILD_PATH',str(ROOT/'PKGBUILD')))
        cmd.add_argument('--index-file',default=os.environ.get('CHATGPT_PACKAGES_FILE'))
        cmd.add_argument('--depends-file',default=str(ROOT/'upstream-depends.txt'))
        cmd.add_argument('--json',action='store_true')
        cmd.add_argument('--workflow',action='store_true',help='structured conservative automation; ordinary CLI stays strict')
    deb=sub.add_parser('deb')
    deb.add_argument('archive'); deb.add_argument('version'); deb.add_argument('depends'); deb.add_argument('layout'); deb.add_argument('destination')
    pkg=sub.add_parser('package')
    pkg.add_argument('--upstream-tree'); pkg.add_argument('archive'); pkg.add_argument('--layout',default=str(ROOT/'upstream-layout.json'))
    args=parser.parse_args()
    try:
        if args.command=='local-sources':
            validate_local_sources(args.pkgbuild)
            return 0
        if args.command in ('check','update'):
            result=discover(args.pkgbuild,args.index_file,args.depends_file,workflow=args.workflow)
            if args.command=='update':
                result['updated']=update(args.pkgbuild,result)
            if args.json:
                print(json.dumps(result,sort_keys=True))
            else:
                for key in ('status','packaged_version','version','filename','sha256'):
                    label='upstream_'+key if key in ('version','filename','sha256') else key
                    print(f'{label}={result[key]}')
            return 10 if not args.workflow and args.command=='check' and result['status']=='outdated' else 0
        if args.command=='deb':
            validate_deb(args.archive,args.version,args.depends,args.layout,args.destination)
        else:
            with open_tar(args.archive) as archive:
                members=inspect_tar(archive,package=True)
                payload_checks(archive,members,json.loads(Path(args.layout).read_text()),package=True)
                if args.upstream_tree:
                    compare_application(archive,members,args.upstream_tree)
            print('package archive validation passed')
        return 0
    except (Invalid, OSError, ValueError, tarfile.TarError, subprocess.CalledProcessError) as exc:
        print(f'upstream: {exc}',file=sys.stderr)
        return 1

if __name__=='__main__':
    sys.exit(main())
