"""Real child CLI -> authenticated temporary HTTP nudge, no model processes."""
from http.server import ThreadingHTTPServer
from pathlib import Path
import json
import os
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch

import test_approved_plan as fixture
import approved_plan as plans
import model_registry as registry
import delegation_policy as policy
import panel_server as panel

REAL_NOTIFY=plans._external_notify


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.PlanTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.scheduled=[];self.paths=[]
        paths=self.paths
        class Handler(panel.Handler):
            def do_GET(self):paths.append(('GET',self.path));super().do_GET()
            def do_POST(self):paths.append(('POST',self.path));super().do_POST()
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler);self.server.daemon_threads=True
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        def cleanup():
            self.server.shutdown();self.server.server_close();self.thread.join(5)
            with plans._LOCK:plans._BINDINGS.clear();plans._ACTIVE.clear();plans._AGAIN.clear()
        self.addCleanup(cleanup)
        self.url='http://127.0.0.1:'+str(self.server.server_port)

    def drain(self):
        count=0
        while self.scheduled:
            fn,args=self.scheduled.pop(0);fn(*args);count+=1;self.assertLess(count,30)

    def seed(self,attach=True):
        self.f.approve()
        if attach:
            plans.attach(self.f.goal,self.f.project,lambda fn,*args:self.scheduled.append((fn,args)),self.f.dispatch)
            self.drain()
        else:plans.advance(self.f.goal,self.f.project,self.f.dispatch)
        for name in self.f.sent:
            run=registry.task_dir(name,self.f.project);registry.patch_task(run,status='completed')
            policy.record_executor(run,{'completed':True,'changed_files':[],'test':{'exit_code':0}})
        self.drain()

    def test_real_cli_acceptance_and_handover_signal_original_live_plan_once(self):
        self.seed();self.assertEqual(self.f.sent,['task-a','task-b']);self.assertEqual(self.scheduled,[])
        source=Path(__file__).resolve().parents[1]
        code=('import sys,json;from pathlib import Path;sys.path.insert(0,sys.argv[1]);'
              'import model_registry as r;r.HERE=Path(sys.argv[2]);r.REGISTRY=r.HERE/"models.json";r.RUNS=r.HERE/"runs";'
              'import delegation_policy as p;import task_handover as h;'
              '\nfor name in ("task-a","task-b"):\n'
              ' run=r.task_dir(name,sys.argv[3]);record=p.load(run);'
              ' criteria=[{"id":c["id"],"status":"pass","evidence":[{"source":"reviewer_inspection","detail":"fixture independent check"}]} for c in record["criteria"]];'
              ' p.accept(run,"main",criteria);'
              ' h.create(run,summary="CLI fixture",test_evidence=[e for c in criteria for e in c["evidence"]])\n')
        result=subprocess.run([sys.executable,'-c',code,str(source),str(self.f.root),self.f.project],
            capture_output=True,text=True,encoding='utf-8',timeout=25,
            env={**os.environ,'MODEL_DESK_PANEL_URL':self.url,'PYTHONIOENCODING':'utf-8'})
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(result.stdout,'')
        self.assertTrue(self.scheduled)
        self.assertEqual(sum(method=='POST' and path.endswith('/signal') for method,path in self.paths),4)
        self.drain();self.assertEqual(self.f.sent,['task-a','task-b','task-c'])
        task=registry.read_task(registry.task_dir('task-a',self.f.project))
        with patch.dict(os.environ,{'MODEL_DESK_PANEL_URL':self.url}):
            self.assertTrue(REAL_NOTIFY(task));self.assertTrue(REAL_NOTIFY(task))
        self.drain();self.assertEqual(self.f.sent,['task-a','task-b','task-c'])

    def test_signal_cannot_reattach_after_restart_or_resume_a_paused_plan(self):
        self.seed(attach=False)
        task=registry.read_task(registry.task_dir('task-a',self.f.project))
        with patch.dict(os.environ,{'MODEL_DESK_PANEL_URL':self.url}):self.assertFalse(REAL_NOTIFY(task))
        self.assertFalse(plans.view(self.f.goal,self.f.project)['attached']);self.assertEqual(self.scheduled,[])
        digest=plans.view(self.f.goal,self.f.project)['plan']['digest']
        plans.change(self.f.goal,self.f.project,'pause',expected=digest)
        before=list(self.paths)
        with patch.dict(os.environ,{'MODEL_DESK_PANEL_URL':self.url}):self.assertFalse(REAL_NOTIFY(task))
        self.assertEqual(self.paths,before)

    def test_nonloopback_or_wrong_installation_never_receives_a_session_credential(self):
        self.seed(attach=False);task=registry.read_task(registry.task_dir('task-a',self.f.project))
        with patch.dict(os.environ,{'MODEL_DESK_PANEL_URL':'https://example.invalid'}),\
                patch('urllib.request.build_opener') as opener:
            self.assertFalse(REAL_NOTIFY(task));opener.assert_not_called()
        class Response:
            def __enter__(self):return self
            def __exit__(self,*_):pass
            def read(self,*_):return json.dumps({'service':'model-desk','ready':True,'approved_plan_notifications':1,'workspace':'C:/other-installation'}).encode()
        with patch.dict(os.environ,{'MODEL_DESK_PANEL_URL':self.url}),patch('urllib.request.build_opener') as factory:
            factory.return_value.open.return_value=Response()
            self.assertFalse(REAL_NOTIFY(task));self.assertEqual(factory.return_value.open.call_count,1)

    def test_signal_requires_original_claimed_task_in_the_same_project(self):
        self.seed();before=list(self.scheduled)
        task=registry.read_task(registry.task_dir('task-a',self.f.project))
        run=registry.prepare_task_dir('unclaimed-task',self.f.project)
        registry.patch_task(run,**{**task,'id':'unclaimed-task'})
        for task_id in ('unclaimed-task','../task-a'):
            with self.subTest(task_id=task_id),self.assertRaises(ValueError):
                plans.signal(self.f.goal,self.f.project,task_id)
        other=Path(self.f.root)/'other-project';other.mkdir()
        with self.assertRaises(ValueError):plans.signal(self.f.goal,str(other),'task-a')
        self.assertEqual(self.scheduled,before)


if __name__=='__main__':unittest.main()
