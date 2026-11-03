"""Formal acceptance uses the existing core, ownership and durable journal."""
import sys
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import brain_context
import brain_gateway as gateway
import brain_requests
import model_registry as registry
import task_integration as integration
import task_leases
from test_task_workspace_handover import Base, git


EVIDENCE = [{'id': 'i1', 'status': 'pass', 'evidence': [
    {'source': 'reviewer_test', 'exit_code': 0, 'detail': 'independently checked the merged fixture'}]}]


class IntegrationAcceptanceGatewayTests(Base):
    def setup_case(self, host='codex'):
        run, project, plan = self.isolated_task(name='accepted-' + host)
        context = brain_context.build_context(project=str(project), host=host,
            owner='owner-' + host, role='brain', operator_session='fixture-' + host,
            session_binding='user_bound')
        registry.patch_task(run, source='brain', owner=context.owner,
            brain={'owner': context.owner, 'project': str(project), 'binding_key': context.binding_key})
        cap = gateway.register_connection(context.declaration())['capability']
        result = gateway.handle('integrate', {'request_id': 'merge-' + host,
            'task_ids': [run.name], 'checks': [[sys.executable, '-c',
                'from pathlib import Path; assert "42" in Path("a.py").read_text()']]}, cap)
        request = {'request_id': 'accept-' + host, 'name': result['integration']['name'],
                   'criteria': EVIDENCE}
        return project, context, cap, request

    def test_both_hosts_accept_replay_and_query_without_running_checks_again(self):
        for host in ('codex', 'dsh'):
            project, context, cap, request = self.setup_case(host)
            before = git(project, 'rev-parse', 'HEAD')
            with patch.object(integration, 'run_integration', side_effect=AssertionError('no remerge')):
                result = gateway.handle('accept_integration', request, cap)
                with patch.object(integration, 'accept_integration', side_effect=AssertionError('no second acceptance')):
                    replay = gateway.handle('accept_integration', request, cap)
            self.assertTrue(result['integration']['accepted'])
            self.assertEqual(replay['integration'], result['integration'])
            self.assertTrue(replay['reused'])
            status = gateway.handle('request_status', {'request_id': request['request_id']}, cap)
            self.assertEqual(status['request_state'], 'completed')
            self.assertEqual(status['request_kind'], 'accept_integration')
            self.assertEqual(status['integration'], result['integration'])
            self.assertEqual(git(project, 'rev-parse', 'HEAD'), before)
            self.assertNotIn(task_leases.key('integration', str(project)), task_leases.holders())

    def test_same_request_different_evidence_refuses(self):
        _, _, cap, request = self.setup_case()
        gateway.handle('accept_integration', request, cap)
        with self.assertRaises(gateway.BrokerError):
            gateway.handle('accept_integration', {**request,
                'criteria': [{**EVIDENCE[0], 'note': 'different payload'}]}, cap)

    def test_foreign_owner_and_project_cannot_accept_or_read_replay(self):
        project, context, cap, request = self.setup_case()
        gateway.handle('accept_integration', request, cap)
        for owner, target in [('other-owner', project), (context.owner, self.make_repo('other-project'))]:
            other = brain_context.build_context(project=str(target), host='dsh', owner=owner,
                role='brain', operator_session='other', session_binding='user_bound')
            other_cap = gateway.register_connection(other.declaration())['capability']
            with self.assertRaises(gateway.BrokerError):
                gateway.handle('accept_integration', request, other_cap)

    def test_bad_evidence_and_names_refuse_before_reservation(self):
        project, context, cap, request = self.setup_case()
        for changes in ({'criteria': []}, {'name': '../escape'}, {'criteria': [
                {'id': 'i1', 'status': 'pass', 'evidence': [{'source': 'self_report', 'detail': 'claim'}]}]}):
            with self.subTest(changes=changes), self.assertRaises((gateway.BrokerError, ValueError)):
                gateway.handle('accept_integration', {**request, **changes}, cap)
            self.assertIsNone(brain_requests.lookup(project, context.binding_key, request['request_id']))

    def test_effect_written_before_reply_failure_is_attributed_to_original_request(self):
        project, _, cap, request = self.setup_case()
        original = integration.accept_integration

        def accept_then_lose_reply(*args, **kwargs):
            original(*args, **kwargs)
            raise OSError('lost reply after durable acceptance')

        with patch.object(integration, 'accept_integration', side_effect=accept_then_lose_reply):
            result = gateway.handle('accept_integration', request, cap)
        self.assertTrue(result['integration']['accepted'])
        self.assertEqual(integration.latest(project, request['name'])['acceptance_request_reference'],
                         result['integration']['acceptance_request_reference'])

    def test_another_completed_acceptance_is_not_recovered_as_this_request(self):
        project, _, cap, request = self.setup_case()
        gateway.handle('accept_integration', request, cap)
        with self.assertRaises(brain_requests.UnknownOutcome):
            gateway.handle('accept_integration', {**request, 'request_id': 'different-accept'}, cap)
        status = gateway.handle('request_status', {'request_id': 'different-accept'}, cap)
        self.assertEqual(status['request_state'], 'unknown')

    def test_existing_project_integration_lease_blocks_acceptance(self):
        project, _, cap, request = self.setup_case()
        task_leases.acquire('integration', str(project), 'other-integration', project=str(project))
        self.addCleanup(lambda: task_leases.release('integration', str(project), 'other-integration'))
        with self.assertRaises(brain_requests.UnknownOutcome):
            gateway.handle('accept_integration', request, cap)
        self.assertFalse(integration.latest(project, request['name'])['accepted'])

    def test_stdio_and_real_http_roundtrip_for_both_hosts(self):
        import panel_server as panel
        from test_brain_mcp_server import StdioClient, payload
        for host in ('codex', 'dsh'):
            project, context, _, request = self.setup_case(host)
            registry.write_json(registry.HERE / 'workflow.json', {'enabled': True})
            server = ThreadingHTTPServer(('127.0.0.1', 0), panel.Handler)
            server.daemon_threads = True
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            client = StdioClient(['--project', str(project), '--host', host,
                '--owner', context.owner, '--role', 'brain', '--session-binding', 'user_bound',
                '--session-id', 'fixture-' + host,
                '--panel-url', 'http://127.0.0.1:' + str(server.server_address[1])],
                registry_home=registry.HERE)
            try:
                client.initialize()
                self.assertFalse(client.call('brain_connect')['isError'])
                with patch.object(panel.EXECUTOR, 'submit', side_effect=AssertionError('no dispatch')):
                    original = integration.latest(project, request['name'])
                    merged = client.call('brain_integrate', {
                        'request_id': 'stdio-merge-' + host, 'task_ids': original['task_ids'],
                        'checks': [[sys.executable, '-c',
                            'from pathlib import Path; assert "42" in Path("a.py").read_text()']]})
                    self.assertFalse(merged['isError'], merged)
                    request = {**request, 'name': payload(merged)['integration']['name']}
                    accepted = client.call('brain_accept_integration', request)
                    self.assertFalse(accepted['isError'], accepted)
                    self.assertTrue(payload(accepted)['integration']['accepted'])
                    replay = client.call('brain_accept_integration', request)
                    self.assertTrue(payload(replay)['reused'])
                    status = client.call('brain_request_status', {'request_id': request['request_id']})
                    self.assertEqual(payload(status)['request_state'], 'completed')
            finally:
                client.close()
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_transport_mutation_results_have_explicit_positive_and_negative_shapes(self):
        from brain_transport import _valid_result
        integrated = {'name': 'fixture', 'verdict': 'passed', 'accepted': False}
        for operation in ('integrate', 'accept_integration'):
            record = integrated if operation == 'integrate' else {
                **integrated, 'accepted': True, 'accepted_commit': 'a' * 40}
            result = {'operation': operation, 'request_id': 'fixture', 'integration': record}
            self.assertTrue(_valid_result(operation, result))
            self.assertFalse(_valid_result(operation, {'context': {}}))
            self.assertFalse(_valid_result(operation, {**result, 'integration': {}}))
        final = {'operation': 'finalize', 'request_id': 'fixture', 'result': {
            'task_id': 'fixture', 'controller_finalization': {'state': 'settled'},
            'executor_replayed': False}}
        self.assertTrue(_valid_result('finalize', final))
        self.assertFalse(_valid_result('finalize', {**final, 'result': {
            **final['result'], 'executor_replayed': True}}))

    def test_every_registered_mutation_has_a_matching_transport_contract(self):
        import brain_transport
        self.assertEqual(brain_context.MUTATING_OPERATIONS, brain_transport.MUTATING_OPERATIONS)
        self.assertEqual(set(gateway.OPERATIONS), brain_context.OPERATIONS)
        integrated = {'name': 'fixture', 'verdict': 'passed', 'accepted': False}
        examples = {
            'dispatch': {'task': {'task_id': 'fixture'}},
            'rework': {'task': {'task_id': 'fixture'}},
            'review': {'receipt': {'review': {}}},
            'integrate': {'operation': 'integrate', 'request_id': 'fixture', 'integration': integrated},
            'accept_integration': {'operation': 'accept_integration', 'request_id': 'fixture',
                'integration': {**integrated, 'accepted': True, 'accepted_commit': 'a' * 40}},
            'finalize': {'operation': 'finalize', 'request_id': 'fixture', 'result': {
                'task_id': 'fixture', 'controller_finalization': {'state': 'settled'},
                'executor_replayed': False}},
            'reconcile_integration': {'operation':'reconcile_integration','request_id':'fixture',
                'integration':{'name':'fixture','queue':{'reconciliation':{'state':'needs_evidence'}}},
                'executor_replayed':False},
        }
        for action in ('draft','prepare','resume','handover'):
            operation='plan_'+action
            examples[operation]={'operation':operation,'request_id':'fixture',
                                 'receipt':{'goal_id':'goal-fixture','action':action}}
        self.assertEqual(set(examples), brain_transport.MUTATING_OPERATIONS)
        for operation, result in examples.items():
            with self.subTest(operation=operation):
                self.assertTrue(brain_transport._valid_result(operation, result))
                self.assertFalse(brain_transport._valid_result(operation, {'context': {}}))

    def test_contradictory_and_mismatched_acceptance_receipts_are_unknown(self):
        import brain_transport
        accepted = {'name': 'fixture', 'verdict': 'passed', 'accepted': True,
                    'accepted_commit': 'a' * 40}
        body = {'request_id': 'review-fixture', 'name': 'fixture', 'criteria': EVIDENCE}
        response = {'operation': 'accept_integration', 'request_id': body['request_id'],
                    'integration': accepted}
        for changes in ({'request_id': 'different-request'},
                        {'integration': {**accepted, 'verdict': 'check_failed'}},
                        {'integration': {**accepted, 'name': 'different-integration'}}):
            transport = brain_transport.BrainTransport()
            transport._capability = 'fixture-only'
            with self.subTest(changes=changes), patch.object(transport, '_request',
                    return_value=(200, {**response, **changes})) as post:
                with self.assertRaises(brain_transport.OutcomeUnknown):
                    transport.call('accept_integration', body)
                self.assertEqual(post.call_count, 1)

    def test_valid_json_with_a_truncated_http_body_is_unknown_without_retry(self):
        import brain_transport

        class TruncatedHandler(BaseHTTPRequestHandler):
            calls = 0

            def log_message(self, *_):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers.get('Content-Length', 0)))
                type(self).calls += 1
                raw = json.dumps({'operation': 'integrate', 'request_id': 'fixture',
                    'integration': {'name': 'fixture', 'verdict': 'passed', 'accepted': False}}).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(raw) + 20))
                self.end_headers()
                self.wfile.write(raw)
                self.close_connection = True

        server = ThreadingHTTPServer(('127.0.0.1', 0), TruncatedHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            transport = brain_transport.BrainTransport('http://127.0.0.1:' + str(server.server_address[1]))
            transport._capability = 'fixture-only'
            with self.assertRaises(brain_transport.OutcomeUnknown):
                transport.call('integrate', {'request_id': 'fixture', 'task_ids': ['fixture'], 'checks': [['git', 'status']]})
            self.assertEqual(TruncatedHandler.calls, 1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_two_simultaneous_reviewers_cannot_share_a_reentrant_lease(self):
        project, context, _, request = self.setup_case()
        entered, release = threading.Event(), threading.Event()
        outcomes = []
        original = integration._accept_integration_locked

        def slow_accept(*args, **kwargs):
            entered.set()
            if not release.wait(10):
                raise AssertionError('test did not release first reviewer')
            return original(*args, **kwargs)

        def first():
            try:
                outcomes.append(integration.accept_integration(project, request['name'], EVIDENCE,
                    authority=brain_context.issue_authority(context)))
            except Exception as error:
                outcomes.append(error)

        with patch.object(integration, '_accept_integration_locked', side_effect=slow_accept) as effect:
            thread = threading.Thread(target=first)
            thread.start()
            try:
                self.assertTrue(entered.wait(10))
                with self.assertRaises(task_leases.LeaseConflict):
                    integration.accept_integration(project, request['name'], EVIDENCE,
                        authority=brain_context.issue_authority(context))
                self.assertEqual(effect.call_count, 1)
            finally:
                release.set()
                thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(outcomes), 1)
        self.assertIsInstance(outcomes[0], dict)
        self.assertTrue(outcomes[0]['accepted'])
        self.assertNotIn(task_leases.key('integration', str(project)), task_leases.holders())

    def test_acceptance_saved_but_journal_save_lost_remains_unknown_without_replaying(self):
        project, context, cap, request = self.setup_case()
        with patch.object(brain_requests, 'complete', side_effect=OSError('fixture journal disk failure')):
            with self.assertRaises(brain_requests.UnknownOutcome):
                gateway.handle('accept_integration', request, cap)
        persisted = integration.latest(project, request['name'])
        self.assertTrue(persisted['accepted'])
        self.assertEqual(persisted['acceptance_request_reference'],
                         brain_requests.reference(context.binding_key, request['request_id']))
        with patch.object(integration, 'accept_integration', side_effect=AssertionError('no repeated effect')):
            with self.assertRaises(brain_requests.UnknownOutcome):
                gateway.handle('accept_integration', request, cap)
        status = gateway.handle('request_status', {'request_id': request['request_id']}, cap)
        self.assertEqual(status['request_state'], 'unknown')
        self.assertFalse(status['safe_to_redispatch'])
        self.assertFalse(status['automatic_retry'])
        self.assertNotIn(task_leases.key('integration', str(project)), task_leases.holders())

    def test_completed_acceptance_survives_connection_registry_reset(self):
        _, context, cap, request = self.setup_case()
        first = gateway.handle('accept_integration', request, cap)
        gateway._CAPABILITIES.clear()
        with self.assertRaises(gateway.AuthError):
            gateway.handle('accept_integration', request, cap)
        fresh_cap = gateway.register_connection(context.declaration())['capability']
        with patch.object(integration, 'accept_integration', side_effect=AssertionError('no repeated effect')):
            recovered = gateway.handle('accept_integration', request, fresh_cap)
        self.assertTrue(recovered['reused'])
        self.assertEqual(recovered['integration'], first['integration'])
