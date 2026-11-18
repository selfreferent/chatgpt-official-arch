#!/usr/bin/env python3
# SPDX-License-Identifier: 0BSD
"""Maintain one deduplicated upstream review issue; API failures are fatal."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys

spec=importlib.util.spec_from_file_location('publisher',Path(__file__).with_name('publish-update.py'))
publisher=importlib.util.module_from_spec(spec); spec.loader.exec_module(publisher)
api=publisher.api
MARKER='<!-- chatgpt-upstream-review:v1 -->'

def fingerprint(report):
    candidate={key:report[key] for key in ('package','version','filename','sha256','depends')}
    findings=sorted(report['findings'],key=lambda f:json.dumps(f,sort_keys=True))
    return hashlib.sha256(json.dumps([candidate,findings],sort_keys=True).encode()).hexdigest()

def details(report):
    # Keep external declarations in a code block, including literal backticks.
    data={key:report.get(key) for key in ('version','sha256','dependency_changes','findings')}
    return '```json\n'+json.dumps(data,indent=2,sort_keys=True).replace('`','\\u0060')+'\n```\n'

def report_review(report):
    issues=[]
    page=1
    while True:
        batch=api('GET',f'/issues?state=all&per_page=100&page={page}')
        issues.extend(i for i in batch if 'pull_request' not in i and MARKER in (i.get('body') or ''))
        if len(batch)<100:
            break
        page+=1
    if len(issues)>1:
        raise ValueError('multiple marked upstream review issues; resolve manually')
    existing=issues[0] if issues else None
    if report['outcome']=='requires_review':
        stamp='<!-- fingerprint:'+fingerprint(report)+' -->'
        body=(MARKER+'\n'+stamp+'\nPublication is blocked pending maintainer review.\n\n'+details(report)+
              '\nValidated base: `'+report['base']+'`\n\nRun: '+os.environ.get('GITHUB_SERVER_URL','https://github.com')+
              '/'+os.environ['GITHUB_REPOSITORY']+'/actions/runs/'+os.environ['GITHUB_RUN_ID']+'\n')
        payload={'title':'Review upstream ChatGPT '+report['version'],'body':body,'state':'open'}
        if existing:
            if stamp not in (existing.get('body') or '') or existing['state']!='open':
                existing=api('PATCH','/issues/'+str(existing['number']),payload)
        else:
            existing=api('POST','/issues',payload)
    elif existing and existing['state']=='open' and report['validation']=='validated':
        existing=api('PATCH','/issues/'+str(existing['number']),{'state':'closed','state_reason':'completed'})
    return existing['html_url'] if existing else None

def main():
    report=json.loads(Path(sys.argv[1]).read_text())
    url=report_review(report)
    print('Commit: `'+report['base']+'`\n')
    print('Outcome: **'+report['outcome']+'**; validation: **'+report['validation']+'**.\n')
    print(details(report))
    if url:
        print('Review issue: '+url+'\n')
    print('Publication: '+('eligible for a validated PR; see publisher job.' if report['outcome']=='candidate' and report['validation']=='validated'
                           else 'none; review required.' if report['outcome']=='requires_review' else 'none; already current.'))

if __name__=='__main__':
    main()
