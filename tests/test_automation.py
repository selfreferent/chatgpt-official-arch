# SPDX-License-Identifier: 0BSD
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from test_upstream import ROOT, DEPS, stanza, u
import test_upstream as fixtures

def load(name, filename):
    spec=importlib.util.spec_from_file_location(name,ROOT/'scripts'/filename)
    module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module

publisher=load('real_publisher','publish-update.py')
reporter=load('reporter','report-review.py')
full=load('full_validation','full-validation.py')

def copy_recipe(work):
    """Keep test minima independent of the currently packaged release."""
    original=(ROOT/'upstream-depends.txt').read_bytes()
    baseline=re.sub(r'libc6 \(>= [^)]+\)', 'libc6 (>= 2.30)', original.decode()).encode()
    text=(ROOT/'PKGBUILD').read_text().replace(hashlib.sha256(original).hexdigest(),hashlib.sha256(baseline).hexdigest())
    (work/'PKGBUILD').write_text(text)
    (work/'upstream-depends.txt').write_bytes(baseline)
    (work/'.SRCINFO').write_bytes(subprocess.check_output(['makepkg','--printsrcinfo'],cwd=work))

class RelaxationTests(unittest.TestCase):
    def test_compatible_lower_minima(self):
        for actual in (DEPS.replace('2.30','2.28'), DEPS.replace('2.30','2.9')):
            changes,eligible=u.dependency_changes(actual,DEPS)
            self.assertTrue(eligible)
            self.assertEqual(changes[0]['removed'],['libc6 (>= 2.30)'])
    def test_other_constraints_require_review(self):
        pairs=[(DEPS.replace('2.30',v),DEPS) for v in ('2.31','2.30.0','1:2.28','2.28-1','2.28~rc1')]
        pairs += [(DEPS.replace('>=','>>'),DEPS),(DEPS+', other',DEPS),
                  (DEPS.replace('libc6','libc6:any'),DEPS),
                  (DEPS.replace('2.30','2.28')+', libc6 (<= 4.0)',DEPS+', libc6 (<= 4.0)'),
                  (DEPS.replace('2.30','2.28'),DEPS+', libc6 (>= 2.29)'),
                  (DEPS.replace('2.30','2.28')+', libc6:any',DEPS+', libc6:any'),
                  ('libc6 (>= 2.28) | alt','libc6 (>= 2.30) | alt'),
                  ('libc6 (>= 2.28)','libc6 (>= 2:2.30)'),
                  ('b | a','a | b')]
        for actual,expected in pairs:
            with self.subTest(actual=actual,expected=expected):
                self.assertFalse(u.dependency_changes(actual,expected)[1])
    def test_malformed_is_error(self):
        with self.assertRaises(u.Invalid):u.dependency_changes('libc6 (??? 2.28)',DEPS)
    def test_workflow_review_does_not_edit(self):
        with tempfile.TemporaryDirectory() as temp:
            work=Path(temp); pkg=work/'PKGBUILD'
            from test_upstream import recipe
            pkg.write_text(recipe()); before=pkg.read_bytes()
            dep=work/'upstream-depends.txt'; dep.write_text(DEPS)
            index=work/'Packages'; index.write_text(stanza('1.2.4',Depends=DEPS+', other'))
            r=u.discover(pkg,index,dep,workflow=True)
            self.assertEqual(r['outcome'],'requires_review'); self.assertTrue(r['findings'])
            self.assertFalse(u.update(pkg,r)); self.assertEqual(pkg.read_bytes(),before)
    def test_structured_layout_and_hard_errors(self):
        tar,layout=fixtures.ArchiveTests().payload({'Exec':'new-command %U'})
        with tar,self.assertRaises(u.ReviewRequired) as caught:
            u.payload_checks(tar,u.inspect_tar(tar),layout)
        self.assertEqual(caught.exception.findings[0]['field'],'Exec')
        tar,layout=fixtures.ArchiveTests().payload(extra={'usr/share/new-integration':b'new'})
        with tar,self.assertRaises(u.ReviewRequired):u.payload_checks(tar,u.inspect_tar(tar),layout)
        tar,layout=fixtures.ArchiveTests().payload({'DBusActivatable':'true'})
        with tar,self.assertRaises(u.ReviewRequired):u.payload_checks(tar,u.inspect_tar(tar),layout)
    def test_namcap_structured_findings_and_parser_failure(self):
        lint=load('structured_namcap','check-namcap.py')
        with tempfile.TemporaryDirectory() as temp:
            log=Path(temp)/'namcap.log'
            log.write_text('package E: missing-required-library NEW\n')
            self.assertEqual(lint.findings([log]),[{'kind':'namcap','severity':'E','diagnostic':'missing-required-library NEW'}])
            log.write_text('Traceback: tool crashed\n')
            with self.assertRaises(lint.Invalid):lint.findings([log])
    def test_full_validation_only_expected_review_is_green(self):
        with tempfile.TemporaryDirectory() as temp:
            report=Path(temp)/'report.json'
            r={'outcome':'candidate','validation':'pending','findings':[]}
            report.write_text(json.dumps(r))
            with patch.object(full,'validate',side_effect=full.u.ReviewRequired([{'kind':'namcap'}])),\
                 patch.object(full.sys,'argv',['full-validation.py','--report',str(report)]):
                self.assertEqual(full.main(),0)
            self.assertEqual(json.loads(report.read_text())['outcome'],'requires_review')
            for error in (full.u.Invalid('checksum mismatch'),subprocess.CalledProcessError(1,'makepkg'),OSError('crash')):
                report.write_text(json.dumps(r))
                with patch.object(full,'validate',side_effect=error),\
                     patch.object(full.sys,'argv',['full-validation.py','--report',str(report)]),self.assertRaises(type(error)):
                    full.main()
                self.assertEqual(json.loads(report.read_text())['validation'],'pending')
            report.write_text(json.dumps(r))
            with patch.object(full,'validate'),patch.object(full.sys,'argv',['full-validation.py','--report',str(report)]):full.main()
            self.assertEqual(json.loads(report.read_text())['validation'],'validated')
    def test_relaxation_stages_consistent_files_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp:
            work=Path(temp)
            copy_recipe(work)
            baseline=(work/'upstream-depends.txt').read_text().replace('2.30','2.28')
            version=u.assignments((work/'PKGBUILD').read_text())['version'][1]
            index=work/'Packages';index.write_text(stanza(version,sha='b'*64,Depends=baseline))
            report=u.discover(work/'PKGBUILD',index,work/'upstream-depends.txt',workflow=True)
            before={name:(work/name).read_bytes() for name in ('PKGBUILD','.SRCINFO','upstream-depends.txt')}
            with patch.object(u.subprocess,'check_output',side_effect=subprocess.CalledProcessError(1,'makepkg')):
                with self.assertRaises(subprocess.CalledProcessError):u.update(work/'PKGBUILD',report)
            self.assertEqual(before,{name:(work/name).read_bytes() for name in before})
            self.assertTrue(u.update(work/'PKGBUILD',report))
            self.assertIn(hashlib.sha256((work/'upstream-depends.txt').read_bytes()).hexdigest(),(work/'PKGBUILD').read_text())
            self.assertEqual((work/'.SRCINFO').read_text(),subprocess.check_output(['makepkg','--printsrcinfo'],cwd=work,text=True))
            again=u.discover(work/'PKGBUILD',index,work/'upstream-depends.txt',workflow=True)
            self.assertEqual(again['outcome'],'no_change');self.assertFalse(u.update(work/'PKGBUILD',again))

class ReviewIssueTests(unittest.TestCase):
    def setUp(self):
        self.report=dict(u.select(stanza()),outcome='requires_review',validation='blocked',base='a'*40,
                         findings=[{'kind':'dependencies','added':['other']}],dependency_changes=[])
        self.env=patch.dict(os.environ,{'GITHUB_REPOSITORY':'owner/repo','GITHUB_RUN_ID':'1'})
        self.env.start(); self.addCleanup(self.env.stop)
    def exercise(self,issues,report=None):
        calls=[]
        def api(method,path,body=None):
            calls.append((method,path,body))
            if method=='GET':return issues
            return dict(number=1,html_url='https://example.com/issue',**body)
        with patch.object(reporter,'api',side_effect=api):reporter.report_review(report or self.report)
        return calls
    def test_create_once_and_deduplicate(self):
        calls=self.exercise([]);self.assertEqual(calls[-1][0],'POST')
        existing=dict(number=1,html_url='url',state='open',body=calls[-1][2]['body'])
        os.environ['GITHUB_RUN_ID']='2'
        self.assertEqual(len(self.exercise([existing])),1)
    def test_changed_candidate_or_findings_update(self):
        body=self.exercise([])[-1][2]['body']
        issue=dict(number=1,html_url='url',state='open',body=body)
        for key,value in [('version','1.2.4'),('findings',[{'kind':'namcap','diagnostic':'new'}])]:
            with self.subTest(key=key):
                r=dict(self.report,**{key:value}); self.assertEqual(self.exercise([issue],r)[-1][0],'PATCH')
    def test_resolve_only_after_validation_and_reopen(self):
        body=self.exercise([])[-1][2]['body']; issue=dict(number=1,html_url='url',state='open',body=body)
        r=dict(self.report,outcome='no_change',validation='pending',findings=[])
        self.assertEqual(len(self.exercise([issue],r)),1)
        r['validation']='validated'
        self.assertEqual(self.exercise([issue],r)[-1][2]['state'],'closed')
        issue['state']='closed'
        self.assertEqual(self.exercise([issue])[-1][2]['state'],'open')
    def test_reporting_failures_are_errors(self):
        for exc in (OSError('authentication'),ValueError('API failed')):
            with patch.object(reporter,'api',side_effect=exc),self.assertRaises(type(exc)):
                reporter.report_review(self.report)
        issue={'body':reporter.MARKER}
        with patch.object(reporter,'api',return_value=[issue,issue]),self.assertRaises(ValueError):reporter.report_review(self.report)
        def api(method,path,body=None):
            if method=='GET':return []
            raise OSError('issue creation failed')
        with patch.object(reporter,'api',side_effect=api),self.assertRaises(OSError):reporter.report_review(self.report)

class RealPublisherTests(unittest.TestCase):
    """Only GitHub API calls are mocked; Git reads, writes and pushes are real."""
    def git(self,*args,cwd=None):
        return subprocess.check_output(['git',*args],cwd=cwd or self.work,text=True,stderr=subprocess.PIPE).strip()
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.work=Path(self.temp.name)/'work'; self.work.mkdir()
        self.remote=Path(self.temp.name)/'remote.git'
        self.environment=patch.dict(os.environ,{'GIT_CONFIG_GLOBAL':'/dev/null','GIT_CONFIG_SYSTEM':'/dev/null',
            'GITHUB_REPOSITORY':'owner/repo','DEFAULT_BRANCH':'main','GIT_TERMINAL_PROMPT':'0'})
        self.environment.start(); self.addCleanup(self.environment.stop)
        self.git('init','--bare',str(self.remote)); self.git('init','-b','main')
        self.git('config','user.name','Fixture');self.git('config','user.email','fixture@example.com')
        copy_recipe(self.work)
        self.git('add','.');self.git('commit','-m','trusted base')
        self.base=self.git('rev-parse','HEAD');self.git('remote','add','origin',str(self.remote));self.git('push','origin','main')
    def prepare(self,relax=False):
        dep=(self.work/'upstream-depends.txt').read_text()
        index=self.work/'Packages';index.write_text(stanza('99.1.1',Depends=dep.replace('2.30','2.28') if relax else dep))
        r=u.discover(self.work/'PKGBUILD',index,self.work/'upstream-depends.txt',workflow=True)
        r['validation']='validated'
        u.update(self.work/'PKGBUILD',r)
        return r
    def publish(self,report,expect_error=False,existing=False,advance_on_read=None):
        handoff=self.work/'validated-update';handoff.mkdir()
        (handoff/'report.json').write_text(json.dumps(report));(handoff/'base.txt').write_text(self.base)
        (handoff/'update.patch').write_text(self.git('diff','--binary')+'\n')
        self.git('restore','--source',self.base,'--worktree','.')
        calls=[]
        def api(method,path,body=None):
            calls.append((method,path,body))
            if method=='GET':
                if advance_on_read:
                    self.git('--git-dir',str(self.remote),'update-ref','refs/heads/main',advance_on_read,self.base)
                return [{'number':1}] if existing else []
            return {'html_url':'https://example.com/pr'}
        previous=Path.cwd()
        try:
            os.chdir(self.work)
            with patch.object(publisher,'api',side_effect=api),contextlib.redirect_stdout(io.StringIO()):
                if expect_error:
                    with self.assertRaises((ValueError,subprocess.CalledProcessError)):publisher.main()
                else:publisher.main()
        finally:os.chdir(previous)
        if expect_error:
            self.assertFalse(self.git('ls-remote','origin','refs/heads/'+publisher.BRANCH))
        else:
            self.assertEqual(self.git('rev-parse','HEAD'),self.git('ls-remote','origin','refs/heads/'+publisher.BRANCH).split()[0])
            self.assertEqual(calls[-1][0],'PATCH' if existing else 'POST')
        return calls
    def test_real_git_publish_and_relaxation(self):
        r=self.prepare(relax=True); self.publish(r)
        self.assertIn('libc6 (>= 2.28)',(self.work/'upstream-depends.txt').read_text())
        self.assertIn("'glibc'",(self.work/'PKGBUILD').read_text())
    def test_real_git_update_existing_pr(self):
        self.git('push','origin','HEAD:refs/heads/'+publisher.BRANCH)
        self.publish(self.prepare(),existing=True)
    def test_tampered_baseline_rejected(self):
        r=self.prepare(relax=True); p=self.work/'upstream-depends.txt';p.write_text(p.read_text()+', other\n')
        self.publish(r,expect_error=True)
    def test_unexpected_baseline_path_rejected(self):
        r=self.prepare(); p=self.work/'upstream-depends.txt';p.write_text(p.read_text()+', other\n')
        self.publish(r,expect_error=True)
    def test_changed_modes_rejected(self):
        r=self.prepare(); (self.work/'PKGBUILD').chmod(0o755); self.publish(r,expect_error=True)
    def test_stale_base_rejected(self):
        r=self.prepare(); self.git('commit','--allow-empty','-m','advance remote');self.git('push','origin','main')
        self.git('reset','--soft',self.base);self.publish(r,expect_error=True)
    def test_base_moving_just_before_push_rejected(self):
        self.git('commit','--allow-empty','-m','future base');future=self.git('rev-parse','HEAD')
        self.git('push','origin','HEAD:refs/heads/future');self.git('reset','--hard',self.base)
        self.publish(self.prepare(),expect_error=True,advance_on_read=future)
    def test_unvalidated_handoff_rejected(self):
        r=self.prepare(); r['validation']='pending';self.publish(r,expect_error=True)
    def test_forged_stronger_requirement_rejected(self):
        r=self.prepare();r['depends']=r['depends'].replace('2.30','2.31');self.publish(r,expect_error=True)
