#!/usr/bin/env python3
# SPDX-License-Identifier: 0BSD
"""Full Arch validation, with typed review results only in workflow mode."""
import argparse
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
import upstream as u

def run(*args, **kwargs):
    return subprocess.run(args,check=True,**kwargs)

def validate():
    u.validate_local_sources('PKGBUILD')
    run('makepkg','--verifysource','--force')
    version=u.assignments(Path('PKGBUILD').read_text())['version'][1]
    # Catch integration review gates before makepkg wraps them in a generic error.
    # Control metadata mismatches and archive safety violations remain hard errors.
    u.validate_deb(f'chatgpt_{version}_amd64.deb',version,'upstream-depends.txt','upstream-layout.json')
    run('makepkg','--cleanbuild','--force')
    packages=subprocess.check_output(['makepkg','--packagelist'],text=True).splitlines()
    u.require(len(packages)==1,'expected exactly one built package')
    run('tests/test-package.sh',packages[0],'src/deb-unpack/data')
    logs=[]
    for name,target in (('recipe','PKGBUILD'),('package',packages[0])):
        path=Path('namcap-'+name+'.log')
        with path.open('w') as log:
            run('namcap','-m',target,stdout=log,stderr=subprocess.STDOUT)
        logs.append(path)
    spec=importlib.util.spec_from_file_location('namcap_policy',ROOT/'scripts/check-namcap.py')
    lint=importlib.util.module_from_spec(spec); spec.loader.exec_module(lint)
    findings=lint.findings(logs)
    if findings:
        raise u.ReviewRequired(findings)
    print('Full source, build, archive, byte comparison, and strict namcap validation passed')

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report',type=Path,help='workflow result to finalize after all checks pass')
    args=parser.parse_args()
    report=json.loads(args.report.read_text()) if args.report else None
    if report is not None:
        u.require(report['outcome'] in ('candidate','no_change') and report['validation']=='pending',
                  'unexpected validation state')
    try:
        validate()
    except u.ReviewRequired as exc:
        if report is None:
            raise
        report.update(outcome='requires_review',validation='blocked',findings=exc.findings)
    else:
        if report is not None:
            report['validation']='validated'
    if report is not None:
        args.report.write_text(json.dumps(report,sort_keys=True)+'\n')
    return 0

if __name__=='__main__':
    sys.exit(main())
