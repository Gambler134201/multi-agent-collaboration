"""Focused tests for the native-entry project binding contract.

The installed host supplies request _meta.threadId even without roots. Directory
metadata is read for that exact thread without requesting chat turns. No process
cwd, service workspace or recent project is used as a substitute.
"""
import asyncio
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parents[1]
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import codex_mcp_server as desk
import project_scope as scope


def make_session(roots=(), capability=True, delay=0.0, capability_error=None):
    async def list_roots():
        if delay:
            await asyncio.sleep(delay)
        return types.SimpleNamespace(roots=list(roots))

    def check(_capability):
        if capability_error is not None:
            raise AssertionError(capability_error)
        return capability

    return types.SimpleNamespace(check_client_capability=check, list_roots=list_roots)


def make_server(session, meta=None, with_meta=True):
    context = types.SimpleNamespace(session=session)
    if with_meta:
        context.meta = meta
    return types.SimpleNamespace(request_context=context)


class SingleRootViewTests(unittest.TestCase):
    def test_missing_ambiguous_and_remote_roots_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = types.SimpleNamespace(uri=Path(folder).as_uri())
            self.assertIsNone(desk.single_root_view([]))
            self.assertIsNone(desk.single_root_view([root, root]))
            self.assertIsNone(desk.single_root_view([types.SimpleNamespace(uri='https://example.com/project')]))
            self.assertIsNone(desk.single_root_view([types.SimpleNamespace(uri='file:///no/such/dir-for-binding-test')]))

    def test_one_local_file_root_is_normalized(self):
        with tempfile.TemporaryDirectory() as folder:
            view = desk.single_root_view([types.SimpleNamespace(uri=Path(folder).as_uri())])
            self.assertEqual(view['mode'], 'project')
            self.assertEqual(scope.project_key(view['project']), scope.project_key(folder))


class OpeningContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_thread_id_resolves_exact_chat_without_roots(self):
        identity = '01a13de0-59f1-7463-b3c1-e2ad78245eee'
        with tempfile.TemporaryDirectory() as folder:
            view = scope.parse_view({'mode':'project','project':folder})
            server = make_server(make_session(capability=False), meta={'threadId':identity})
            with mock.patch.object(desk, 'read_thread_project', return_value=view) as lookup:
                self.assertEqual(await desk.opening_view(server, {}), view)
                lookup.assert_called_once_with(identity)

    async def test_identified_unreadable_thread_never_falls_back_to_another_root(self):
        identity = '01a13de0-59f1-7463-b3c1-e2ad78245eee'
        with tempfile.TemporaryDirectory() as folder:
            server = make_server(make_session(roots=[types.SimpleNamespace(uri=Path(folder).as_uri())]),
                                 meta={'threadId':identity})
            for outcome in (None, OSError('unavailable')):
                with mock.patch.object(desk, 'read_thread_project', **(
                        {'side_effect':outcome} if isinstance(outcome, Exception) else {'return_value':outcome})):
                    self.assertEqual(await desk.opening_view(server, {}), {'mode':'unbound','project':None})

    async def test_project_argument_cannot_spoof_native_thread_identity(self):
        with mock.patch.object(desk, 'read_thread_project') as lookup:
            self.assertEqual(await desk.opening_view(make_server(make_session(capability=False)),
                {'threadId':'01a13de0-59f1-7463-b3c1-e2ad78245eee'}), {'mode':'unbound','project':None})
            lookup.assert_not_called()

    async def test_explicit_project_wins_and_never_asks_for_roots(self):
        with tempfile.TemporaryDirectory() as folder:
            expected = os.path.normcase(os.path.realpath(folder))
            session = make_session(capability_error='roots must not be consulted for an explicit project')
            view = await desk.opening_view(make_server(session), {'project': folder})
        self.assertEqual(view['mode'], 'project')
        self.assertEqual(os.path.normcase(view['project']), expected)

    async def test_invalid_explicit_project_is_rejected_instead_of_guessed(self):
        with tempfile.TemporaryDirectory() as folder:
            missing = str(Path(folder) / 'missing')
            with self.assertRaises(ValueError):
                await desk.opening_view(make_server(make_session()), {'project': missing})
            with self.assertRaises(ValueError):
                await desk.opening_view(make_server(make_session()), {'project': 'https://example.com/project'})

    async def test_single_advertised_root_binds(self):
        with tempfile.TemporaryDirectory() as folder:
            expected = os.path.normcase(os.path.realpath(folder))
            roots = [types.SimpleNamespace(uri=Path(folder).as_uri())]
            view = await desk.opening_view(make_server(make_session(roots=roots)), {})
        self.assertEqual(view['mode'], 'project')
        self.assertEqual(os.path.normcase(view['project']), expected)

    async def test_no_roots_capability_stays_unbound(self):
        with tempfile.TemporaryDirectory() as folder:
            view = await desk.opening_view(make_server(make_session(roots=[types.SimpleNamespace(uri=Path(folder).as_uri())], capability=False)), {})
        self.assertEqual(view, {'mode': 'unbound', 'project': None})

    async def test_ambiguous_remote_and_timeout_stay_unbound(self):
        with tempfile.TemporaryDirectory() as folder:
            root = types.SimpleNamespace(uri=Path(folder).as_uri())
            ambiguous = await desk.opening_view(make_server(make_session(roots=[root, root])), {})
            remote = await desk.opening_view(make_server(make_session(roots=[types.SimpleNamespace(uri='https://example.com/project')])), {})
            slow = await desk.opening_view(make_server(make_session(roots=[root], delay=3)), {})
        for view in (ambiguous, remote, slow):
            self.assertEqual(view, {'mode': 'unbound', 'project': None})

    async def test_native_entrypoint_shape_stays_unbound(self):
        """Empty arguments, no roots capability, thread meta and a local cwd."""
        with tempfile.TemporaryDirectory() as folder:
            meta = types.SimpleNamespace(model_dump=lambda: {
                'progressToken': 1, 'threadId': 'thread-a', 'thread_id': 'thread-a',
                'cwd': folder, 'workspaceRoots': [folder], 'project': folder,
            })
            server = make_server(make_session(capability=False), meta=meta)
            with mock.patch.object(desk.os, 'getcwd', side_effect=AssertionError('cwd must never be consulted')):
                view = await desk.opening_view(server, {})
        self.assertEqual(view, {'mode': 'unbound', 'project': None})

    async def test_unknown_meta_fields_are_not_a_project_source(self):
        server = make_server(make_session(capability=False), meta={'threadId': 't', 'cwd': 'E:\\somewhere', 'project': 'E:\\somewhere'})
        self.assertEqual(await desk.opening_view(server, {}), {'mode': 'unbound', 'project': None})

    async def test_missing_request_context_is_not_an_error(self):
        server = types.SimpleNamespace()
        self.assertEqual(await desk.opening_view(server, {}), {'mode': 'unbound', 'project': None})


class NativeEntryHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_open_tool_from_a_contextless_host_stays_unbound(self):
        from mcp import types as mcp_types

        server = desk.build_server()
        request = mcp_types.CallToolRequest(method='tools/call', params=mcp_types.CallToolRequestParams(
            name=desk.TOOL_OPEN, arguments={}))
        with mock.patch.object(desk, 'ensure_service', return_value=True):
            result = await server.request_handlers[mcp_types.CallToolRequest](request)
        payload = result.root.structuredContent
        self.assertFalse(result.root.isError)
        self.assertEqual(payload['view'], {'mode': 'unbound', 'project': None})

    async def test_open_tool_keeps_an_explicit_project(self):
        from mcp import types as mcp_types

        with tempfile.TemporaryDirectory() as folder:
            expected = os.path.normcase(os.path.realpath(folder))
            server = desk.build_server()
            request = mcp_types.CallToolRequest(method='tools/call', params=mcp_types.CallToolRequestParams(
                name=desk.TOOL_OPEN, arguments={'project': folder}))
            with mock.patch.object(desk, 'ensure_service', return_value=True):
                result = await server.request_handlers[mcp_types.CallToolRequest](request)
            view = result.root.structuredContent['view']
        self.assertEqual(view['mode'], 'project')
        self.assertEqual(os.path.normcase(view['project']), expected)


class ThreadMetadataTests(unittest.TestCase):
    def test_exact_id_and_local_directory_required(self):
        with tempfile.TemporaryDirectory() as folder:
            valid = {'id':'native-thread', 'cwd':folder}
            self.assertEqual(desk.thread_metadata_view('native-thread', valid)['mode'], 'project')
            self.assertIsNone(desk.thread_metadata_view('another-thread', valid))
            for directory in ('relative', 'https://example.com/repo', str(Path(folder)/'missing')):
                self.assertIsNone(desk.thread_metadata_view('native-thread', {**valid,'cwd':directory}))
            for environments in ([], [{'environmentId':'remote','cwd':folder}],
                                 [{'environmentId':'local','cwd':folder}]*2):
                self.assertIsNone(desk.thread_metadata_view('native-thread', {**valid,'environments':environments}))
            self.assertEqual(desk.thread_metadata_view('native-thread', {**valid,'environments':[
                {'environmentId':'local','cwd':folder}]})['mode'], 'project')

    def test_lookup_uses_only_initialization_and_metadata_read(self):
        import io
        class Input(io.StringIO):
            def close(self):
                pass
        with tempfile.TemporaryDirectory() as folder:
            process = types.SimpleNamespace(stdin=Input(), stdout=io.StringIO(
                json.dumps({'id':1,'result':{}})+'\n'+json.dumps({'id':2,'result':{
                    'thread':{'id':'native-thread','cwd':folder}}})+'\n'), wait=mock.Mock())
            with mock.patch.object(desk.subprocess,'Popen',return_value=process), \
                 mock.patch.object(desk,'find_codex',return_value='codex.exe'):
                self.assertEqual(desk.read_thread_project('native-thread')['mode'],'project')
            requests = [json.loads(line) for line in process.stdin.getvalue().splitlines()]
            self.assertEqual([r['method'] for r in requests], ['initialize','initialized','thread/read'])
            self.assertEqual(requests[-1]['params'],{'threadId':'native-thread','includeTurns':False})


if __name__ == '__main__':
    unittest.main(verbosity=2)
