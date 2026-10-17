"""Real hidden listener and native framing against a fake Windows pipe host.

All records and identities are temporary. No real desktop pipe, worker service,
model provider or inference is used. MODEL_DESK_LONG_WAIT_TEST=1 tests 75 seconds.
"""
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import completion_wait as wait
import desktop_notify as desktop


class FakeDesktop:
    def __init__(self, project, identity):
        self.project=project; self.identity=identity
        self.name=r'\\.\pipe\model-desk-isolated-'+uuid.uuid4().hex
        self.stopped=threading.Event();self.ready=threading.Event();self.events=[];self.errors=[]
        self.kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        self.kernel.CreateNamedPipeW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,wintypes.DWORD,wintypes.DWORD,wintypes.DWORD,wintypes.DWORD,ctypes.c_void_p]
        self.kernel.CreateNamedPipeW.restype=wintypes.HANDLE
        self.kernel.ConnectNamedPipe.argtypes=[wintypes.HANDLE,ctypes.c_void_p]
        self.kernel.ReadFile.argtypes=[wintypes.HANDLE,ctypes.c_void_p,wintypes.DWORD,ctypes.POINTER(wintypes.DWORD),ctypes.c_void_p]
        self.kernel.WriteFile.argtypes=self.kernel.ReadFile.argtypes
        for name in ['FlushFileBuffers','DisconnectNamedPipe','CloseHandle']:
            getattr(self.kernel,name).argtypes=[wintypes.HANDLE]
        self.thread=threading.Thread(target=self.serve,daemon=True)

    def start(self):
        self.thread.start()
        if not self.ready.wait(3):raise AssertionError('Fixture pipe did not start')

    def read_exact(self,handle,count):
        result=b''
        while len(result)<count:
            buffer=ctypes.create_string_buffer(count-len(result)); got=wintypes.DWORD()
            if not self.kernel.ReadFile(handle,buffer,len(buffer),ctypes.byref(got),None) or not got.value:
                raise OSError('Fixture client disconnected')
            result+=buffer.raw[:got.value]
        return result

    def serve(self):
        while not self.stopped.is_set():
            handle=self.kernel.CreateNamedPipeW(self.name,3,0,1,65536,65536,1000,None)
            if handle==ctypes.c_void_p(-1).value:
                self.errors.append('fixture_pipe_creation');self.ready.set();return
            self.ready.set()
            try:
                connected=self.kernel.ConnectNamedPipe(handle,None)
                if not connected and ctypes.get_last_error()!=535:raise OSError('Fixture connect failed')
                if self.stopped.is_set():break
                size=struct.unpack('<I',self.read_exact(handle,4))[0]
                if size>65536:raise ValueError('Oversize fixture request')
                request=json.loads(self.read_exact(handle,size));params=request['params'];tool=params['tool']
                assert params['threadId']==self.identity==params['arguments']['threadId']
                assert request['method']=='tools/call'
                self.events.append({'tool':tool,'at':time.monotonic()})
                if tool=='read_thread':
                    data={'thread':{'id':self.identity,'cwd':self.project},
                          'turns':[{'id':'fixture-original-turn','status':'completed','startedAt':1,'completedAt':2}]}
                    result={'success':True,'contentItems':[{'type':'inputText','text':json.dumps(data)}]}
                elif tool=='send_message_to_thread':
                    assert params['arguments']['prompt'].startswith('[Model Desk completion event] ')
                    result={'success':True,'contentItems':[]}
                else:raise AssertionError('Fixture permits no other host operation')
                payload=json.dumps({'jsonrpc':'2.0','id':request['id'],'result':result}).encode()
                outgoing=struct.pack('<I',len(payload))+payload
                buffer=ctypes.create_string_buffer(outgoing);written=wintypes.DWORD()
                if not self.kernel.WriteFile(handle,buffer,len(outgoing),ctypes.byref(written),None):raise OSError('Fixture write failed')
                self.kernel.FlushFileBuffers(handle)
            except Exception as error:
                self.errors.append(type(error).__name__)
            finally:
                self.kernel.DisconnectNamedPipe(handle);self.kernel.CloseHandle(handle)

    def stop(self):
        self.stopped.set()
        try:
            with open(self.name,'r+b',buffering=0):pass
        except OSError:pass
        self.thread.join(3)
        if self.thread.is_alive():raise AssertionError('Fixture pipe thread did not stop')


@unittest.skipUnless(os.name=='nt','Windows native pipe integration')
class HiddenCompletionProcessTest(unittest.TestCase):
    def test_detached_listener_waits_without_host_calls_then_delivers_one_batch(self):
        with tempfile.TemporaryDirectory(prefix='model-desk-completion-process-') as temporary:
            project=Path(temporary)/'项目';project.mkdir()
            identity=str(uuid.uuid4())
            host=FakeDesktop(str(project),identity);host.start()
            environment=desktop.child_environment()
            environment.update(CODEX_THREAD_ID=identity,CODEX_APP_TOOLS_PIPE_PATH=host.name,PYTHONIOENCODING='utf-8')
            for task_id in ('fixture-a','fixture-b'):
                run=project/'.model-desk/runs'/task_id;run.mkdir(parents=True)
                wait.registry.write_json(run/'task.json',{'id':task_id,'project':str(project),'status':'running','timeout':0})
            subscription=None
            try:
                result=subprocess.run([sys.executable,str(Path(wait.__file__).resolve()),'--project',str(project),
                                       '--tasks','fixture-a','fixture-b'],cwd=project,env=environment,
                                      capture_output=True,text=True,encoding='utf-8',timeout=15,**desktop.hidden_options())
                self.assertEqual(result.returncode,0,result.stderr)
                subscription=json.loads(result.stdout)
                self.assertEqual(subscription['listener_status'],'listening')
                self.assertFalse(subscription['console_window_present'])
                root=project/'.model-desk/notifications'/subscription['id']
                began=time.monotonic()
                duration=75 if os.environ.get('MODEL_DESK_LONG_WAIT_TEST')=='1' else 2
                time.sleep(duration)
                elapsed=time.monotonic()-began
                # The only native request so far is the parent's preflight.
                self.assertEqual([event['tool'] for event in host.events],['read_thread'])
                first=project/'.model-desk/runs/fixture-a/task.json'
                data=wait.registry.read_json(first);data.update(status='completed',ended_at=wait.now());wait.registry.write_json(first,data)
                time.sleep(1.1)
                self.assertFalse(any(event['tool']=='send_message_to_thread' for event in host.events))
                second=project/'.model-desk/runs/fixture-b/task.json'
                data=wait.registry.read_json(second);data.update(status='completed',ended_at=wait.now());wait.registry.write_json(second,data)
                deadline=time.monotonic()+8
                while time.monotonic()<deadline:
                    current=wait.registry.read_json(root/'subscription.json')
                    if current['listener_status']=='delivered':break
                    time.sleep(.1)
                self.assertEqual(current['listener_status'],'delivered',current)
                self.assertEqual(current['phase'],'awaiting_review')
                self.assertEqual(current['send_attempts'],1)
                self.assertEqual(sum(event['tool']=='send_message_to_thread' for event in host.events),1)
                self.assertEqual(wait.registry.read_json(first)['timeout'],0)
                self.assertFalse(host.errors,host.errors)
                report={'passed':True,'wait_seconds':round(elapsed,3),'native_requests_during_wait':0,
                        'completion_notifications':1,'console_window_present':False,'real_provider_calls':0,
                        'real_desktop_calls':0,'task_runtime_limit':0,'fake_host':True}
                destination=os.environ.get('MODEL_DESK_PROCESS_REPORT')
                if destination:Path(destination).write_text(json.dumps(report,indent=2),encoding='utf-8')
            finally:
                if subscription:
                    root=project/'.model-desk/notifications'/subscription['id']
                    (root/'stop.flag').touch()
                    deadline=time.monotonic()+4
                    while wait.process_alive(subscription.get('watcher_pid')) is True and time.monotonic()<deadline:
                        time.sleep(.1)
                host.stop()


if __name__=='__main__':unittest.main()
