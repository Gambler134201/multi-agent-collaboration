"""Shared readiness survives real stdio/HTTP projection, without model work."""
from http.server import ThreadingHTTPServer
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import endpoint_readiness as readiness
import brain_gateway as gateway
import execution_endpoints as endpoints
import execution_engines as engines
import model_registry as registry
import panel_server as panel
import test_brain_mcp_server as protocol


class ReadinessProtocolTests(unittest.TestCase):
    def test_both_hosts_read_candidates_and_preflight_without_dispatch_or_settings_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);project=root/'project';project.mkdir()
            model={'id':'local','name':'Local fixture','provider':'local','model':'fixture',
                   'enabled':True,'default_agent':'pi','max_input_tokens':32768,'max_output_tokens':4096}
            controls={'readable':True,'error':'','endpoints':endpoints.defaults()}
            for value in controls['endpoints'].values():value['enabled']=True
            runtime={'ready':False,'readiness':readiness.runtime('configuration_missing')}
            with patch.object(registry,'HERE',root),patch.object(registry,'REGISTRY',root/'models.json'),\
                    patch.object(registry,'RUNS',root/'runs'),\
                    patch.dict(gateway._CAPABILITIES,clear=True),\
                    patch.object(endpoints,'snapshot',return_value=controls),\
                    patch.object(engines,'availability',return_value=runtime),\
                    patch.object(panel.EXECUTOR,'submit') as submit:
                registry.write_json(root/'workflow.json',{'enabled':True,'execution_agent':'pi'})
                registry.write_json(registry.REGISTRY,{'default_model':'local','models':[model]})
                originals={p.name:p.read_bytes() for p in (registry.REGISTRY,root/'workflow.json')}
                server=ThreadingHTTPServer(('127.0.0.1',0),panel.Handler)
                server.daemon_threads=True
                thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
                seen=[]
                try:
                    for host in ('codex','dsh'):
                        client=protocol.StdioClient([
                            '--project',str(project),'--host',host,'--owner','readiness-fixture',
                            '--role','brain','--session-binding','user_bound','--session-id','fixture-'+host,
                            '--panel-url','http://127.0.0.1:%d'%server.server_address[1]],registry_home=root)
                        try:
                            client.initialize()
                            self.assertFalse(client.call('brain_connect')['isError'])
                            response=client.call('brain_candidates')
                            self.assertFalse(response['isError'])
                            candidates=protocol.visible_payload(response)
                            self.assertEqual(candidates,protocol.payload(response))
                            row=next(r for r in candidates['execution_endpoints']['rows'] if r['id']=='pi')
                            self.assertEqual(row['diagnostic']['code'],'configuration_missing')
                            self.assertIn('MODEL_DESK_PI_HOME',row['diagnostic']['next_step'])
                            self.assertFalse(row['diagnostic']['precheck_passed'])
                            self.assertNotIn('runtime',row);self.assertNotIn('models',row)
                            preflight=client.call('brain_preflight')
                            self.assertFalse(preflight['isError'])
                            data=protocol.visible_payload(preflight)
                            self.assertEqual(data['candidates']['execution_endpoints'],candidates['execution_endpoints'])
                            self.assertFalse(data['model_called']);self.assertFalse(data['task_dispatched'])
                            self.assertNotEqual(data['user_signal']['state'],'ready')
                            seen.append(candidates['execution_endpoints'])
                        finally:client.close()
                    self.assertEqual(seen[0],seen[1])
                    submit.assert_not_called()
                    self.assertFalse((project/'.model-desk').exists())
                    for name,value in originals.items():self.assertEqual((root/name).read_bytes(),value)
                finally:
                    server.shutdown();server.server_close();thread.join(timeout=5)


if __name__=='__main__':unittest.main()
