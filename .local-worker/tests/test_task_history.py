"""Permanent cleanup tests use temporary runs/SQLite/projects exclusively."""
import json
import os
from pathlib import Path
import time
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request

from test_panel_observability import Base
import model_registry as registry
import stage_store
import task_history as history
import project_scope
import codex_mcp_server as desk


class HistoryTests(Base):
    def setUp(self):
        super().setUp();history.PREVIEWS.clear()
        self.other=Path(self.temp.name)/'other';self.other.mkdir()
        self.view={'mode':'project','project':str(self.project)}
        self.task('a',project=str(self.project));self.task('b',project=str(self.other))
        (registry.task_dir('a')/'prompt.txt').write_text('PRIVATE_PROMPT')
        self.ledger=registry.HERE/'budget-ledger.json';self.ledger.write_text('{"retained_cost":12.34}')

    def sql(self, query, args=()):
        with stage_store.connection() as conn:return conn.execute(query,args).fetchall()

    def complete_group(self):
        goal=stage_store.create_goal('Finished goal','objective',str(self.project))
        first=stage_store.add_stage(goal['id'],'one','objective',acceptance=['passes'],candidates=['home'])
        second=stage_store.add_stage(goal['id'],'two','objective',depends_on=[first['id']],acceptance=['passes'],candidates=['home'])
        self.task('c',project=str(self.project),stage_id=second['id'],goal_id=goal['id'])
        registry.patch_task(registry.task_dir('a'),stage_id=first['id'],goal_id=goal['id'])
        for stage,key in [(first,'a'),(second,'c')]:
            self.sql('UPDATE stages SET status="accepted",task_ids=?,active_task_id=? WHERE id=?',(json.dumps([key]),key,stage['id']))
        stage_store.set_goal_status(goal['id'],'completed')
        return goal,first,second

    def test_permanent_delete_and_account_project_isolation(self):
        before=self.ledger.read_bytes();project_file=(self.project/'a.py').read_bytes()
        listing=history.listing(self.view)
        self.assertEqual([row['id'] for row in listing['tasks']],['a'])
        self.assertNotIn('PRIVATE_PROMPT',json.dumps(listing))
        preview=history.preview(self.view,['a']);self.assertEqual(preview['added_ids'],[])
        result=history.delete(self.view,preview['token'])
        self.assertEqual(result['deleted_tasks'],1);self.assertFalse(result['cleanup_pending'])
        self.assertFalse(registry.task_dir('a').exists());self.assertTrue(registry.task_dir('b').exists())
        with self.assertRaises(FileNotFoundError):registry.read_task(registry.task_dir('a'))
        self.assertFalse(registry.task_dir('a').exists())
        self.assertEqual(self.ledger.read_bytes(),before);self.assertEqual((self.project/'a.py').read_bytes(),project_file)
        self.assertEqual(list(history.staging_root().iterdir()),[])
        self.assertEqual(self.sql('SELECT phase FROM task_deletion_log')[0]['phase'],'purged')
        with self.assertRaises(ValueError):history.delete(self.view,preview['token'])

    def test_project_scope_invalid_ids_token_and_stale_preview(self):
        for ids in ([],['b'],['../a'],['unknown'],[4]):
            with self.assertRaises(ValueError):history.preview(self.view,ids)
        for view in ({'mode':'all'},{'mode':'unbound'}):
            with self.assertRaises(ValueError):history.listing(view)
        preview=history.preview(self.view,['a'])
        with self.assertRaises(ValueError):history.delete({'mode':'project','project':str(self.other)},preview['token'])
        registry.patch_task(registry.task_dir('a'),title='changed')
        with self.assertRaisesRegex(ValueError,'变化'):history.delete(self.view,preview['token'])
        self.assertTrue(registry.task_dir('a').exists())
        preview=history.preview(self.view,['a']);history.PREVIEWS[preview['token']]['_created']-=301
        with self.assertRaisesRegex(ValueError,'过期'):history.delete(self.view,preview['token'])

    def test_active_queue_and_live_guard_prevent_partial_delete(self):
        preview=history.preview(self.view,['a'])
        registry.patch_task(registry.task_dir('b'),status='running')
        # A fresh preview may be made while another project runs, but commit cannot.
        preview=history.preview(self.view,['a'])
        with self.assertRaisesRegex(ValueError,'队列空闲'):history.delete(self.view,preview['token'])
        registry.patch_task(registry.task_dir('b'),status='completed')
        preview=history.preview(self.view,['a'])
        with self.assertRaisesRegex(ValueError,'live fixture'):
            history.delete(self.view,preview['token'],idle_guard=mock.Mock(side_effect=ValueError('live fixture')))
        self.assertTrue(registry.task_dir('a').exists())

    def test_completed_goal_cascade_and_active_goal_protection(self):
        goal,first,second=self.complete_group()
        preview=history.preview(self.view,['a'])
        self.assertEqual(preview['added_ids'],['c']);self.assertEqual(preview['stage_count'],2)
        history.delete(self.view,preview['token'])
        self.assertFalse(registry.task_dir('c').exists());self.assertEqual(self.sql('SELECT * FROM goals'),[])
        self.assertEqual(self.sql('SELECT * FROM stages'),[]);self.assertEqual(self.sql('SELECT * FROM stage_log'),[])
        goal=stage_store.create_goal('Active','objective',str(self.project))
        self.task('d',project=str(self.project),goal_id=goal['id'])
        self.assertFalse(history.listing(self.view)['tasks'][0]['deletable'])
        with self.assertRaisesRegex(ValueError,'仍在进行'):history.preview(self.view,['d'])

    def test_surviving_references_expand_or_block_cross_project(self):
        self.task('c',project=str(self.project),parent_task_id='a')
        self.assertEqual(history.preview(self.view,['a'])['added_ids'],['c'])
        registry.patch_task(registry.task_dir('b'),rework_of='a')
        with self.assertRaisesRegex(ValueError,'跨项目'):history.preview(self.view,['a'])

    def test_link_and_project_folder_are_never_recursively_deleted(self):
        contained=registry.task_dir('a')/'workspace';contained.mkdir()
        self.task('inside',project=str(contained))
        with self.assertRaisesRegex(ValueError,'项目文件'):history.preview(self.view,['a'])
        self.task('link-run',project=str(self.project))
        # A file symlink is sufficient to exercise no-follow guarding without
        # requiring the host's directory-symlink privilege in this fixture.
        with mock.patch.object(history.os.path,'isjunction',side_effect=lambda path:Path(path)==registry.task_dir('link-run')):
            with self.assertRaisesRegex(ValueError,'链接'):history.preview(self.view,['link-run'])
        self.assertTrue(contained.exists())

    def test_bookmarked_project_without_tasks_is_not_deleted_with_parent_run(self):
        import project_views
        contained=registry.task_dir('a')/'user-project';contained.mkdir()
        marker=contained/'keep.txt';marker.write_text('project file')
        project_views.remember_project({'mode':'project','project':str(contained)})
        with self.assertRaisesRegex(ValueError,'项目文件'):history.preview(self.view,['a'])
        self.assertEqual(marker.read_text(),'project file')

    def test_move_failure_rolls_back_files_and_sql(self):
        goal,_,_=self.complete_group();preview=history.preview(self.view,['a'])
        rename=os.rename
        def failure(source,dest):
            if Path(source)==registry.task_dir('c'):raise PermissionError('fixture locked')
            return rename(source,dest)
        with mock.patch.object(history.os,'rename',side_effect=failure):
            with self.assertRaises(PermissionError):history.delete(self.view,preview['token'])
        self.assertTrue(registry.task_dir('a').exists());self.assertTrue(registry.task_dir('c').exists())
        self.assertEqual(len(self.sql('SELECT * FROM goals')),1);self.assertEqual(list(history.staging_root().iterdir()),[])

    def test_recovery_restores_uncommitted_and_purges_committed(self):
        empty=history.staging_root()/('e'*32);empty.mkdir(parents=True)
        history.recover();self.assertFalse(empty.exists())
        job=history.staging_root()/('a'*32);job.mkdir(parents=True)
        registry.write_json(job/'manifest.json',{'ids':['a']});os.rename(registry.task_dir('a'),job/'a')
        history.recover();self.assertTrue(registry.task_dir('a').exists());self.assertFalse(job.exists())
        preview=history.preview(self.view,['a'])
        with mock.patch.object(history.shutil,'rmtree',side_effect=PermissionError('fixture locked')):
            result=history.delete(self.view,preview['token'])
        self.assertTrue(result['cleanup_pending']);self.assertFalse(registry.task_dir('a').exists())
        history.recover();self.assertEqual(list(history.staging_root().iterdir()),[])
        self.assertEqual(self.sql('SELECT phase FROM task_deletion_log')[0]['phase'],'purged')

    def test_live_activity_stays_global_while_history_is_project_scoped(self):
        import panel_server as panel
        registry.patch_task(registry.task_dir('b'),status='running')
        active=panel.execution_activity()
        self.assertEqual(active['scope'],'global');self.assertTrue(active['complete'])
        self.assertEqual([row['id'] for row in active['tasks']],['b'])
        self.assertEqual([row['id'] for row in history.listing(self.view)['tasks']],['a'])
        self.assertNotIn('answer',active['tasks'][0]);self.assertNotIn('prompt',active['tasks'][0])

    def test_more_than_150_history_records_are_not_silently_omitted(self):
        for i in range(155):self.task('extra-'+str(i),project=str(self.project))
        result=history.listing(self.view)
        self.assertEqual(result['total'],156)
        self.assertEqual(len(result['tasks']),156)

    def test_project_store_deletion_cleans_index_and_project_db(self):
        import project_storage
        project=str(self.project)
        run=registry.prepare_task_dir('p1',self.project)
        registry.patch_task(run,id='p1',title='项目任务',kind='edit',engine='dsh',model_id='home',
                            model_name='家中的 Qwen',model='qwen-test',status='completed',phase='完成',
                            project=project,created_at=registry.now())
        goal=stage_store.create_goal('项目目标','objective',project,store='project')
        stage=stage_store.add_stage(goal['id'],'阶段','objective',acceptance=['passes'],candidates=['home'])
        registry.patch_task(run,goal_id=goal['id'],stage_id=stage['id'])
        with stage_store.connection(project) as conn:
            conn.execute('UPDATE stages SET status="accepted",task_ids=?,active_task_id=? WHERE id=?',
                         (json.dumps(['p1']),'p1',stage['id']))
        stage_store.set_goal_status(goal['id'],'completed')
        listing=history.listing(self.view)
        self.assertIn('p1',[row['id'] for row in listing['tasks']])
        preview=history.preview(self.view,['p1'])
        self.assertEqual(preview['added_ids'],[])
        result=history.delete(self.view,preview['token'])
        self.assertEqual(result['deleted_tasks'],1)
        self.assertFalse(project_storage.task_dir(project,'p1').exists())
        self.assertIsNone(project_storage.locate('task','p1'))
        with stage_store.connection(project) as conn:
            self.assertIsNone(conn.execute('SELECT id FROM goals WHERE id=?',(goal['id'],)).fetchone())
            self.assertIsNone(conn.execute('SELECT id FROM stages WHERE id=?',(stage['id'],)).fetchone())
        self.assertTrue(registry.task_dir('a').exists())

    def test_history_listing_reads_the_project_store_without_the_locator(self):
        import project_storage
        project=self.project
        run=registry.prepare_task_dir('own-1',project)
        registry.patch_task(run,id='own-1',title='项目记录',kind='edit',engine='dsh',model_id='home',
                            model_name='家中的 Qwen',model='qwen-test',status='completed',phase='完成',
                            project=str(project),created_at=registry.now())
        self.assertEqual(sorted(row['id'] for row in history.listing(self.view)['tasks']),
                         ['a','own-1'])
        project_storage.index_path().unlink()
        listing=history.listing(self.view)
        self.assertIn('own-1',[row['id'] for row in listing['tasks']])
        self.assertNotIn('b',[row['id'] for row in listing['tasks']],
                         '项目视图不得读取其他项目的记录')
        preview=history.preview(self.view,['own-1'])
        self.assertEqual(preview['requested_ids'],['own-1'])
        result=history.delete(self.view,preview['token'])
        self.assertEqual(result['deleted_tasks'],1)
        self.assertFalse(project_storage.task_dir(project,'own-1').exists())
        self.assertIsNone(project_storage.locate('task','own-1'))
        self.assertTrue(registry.task_dir('b').exists())

    def test_http_requires_session_and_scoped_view_mcp_is_concrete(self):
        def call(path,body=None,auth=True,view=None):
            selected=self.view if view is None else view
            headers={'Content-Type':'application/json','X-Model-Desk-View':project_scope.encode_view_header(selected)}
            if auth:headers['X-Panel-Token']=__import__('panel_server').TOKEN
            req=Request(self.url+path,headers=headers,data=None if body is None else json.dumps(body).encode())
            with self.opener.open(req,timeout=15) as response:return json.load(response)
        self.assertEqual(call('/api/task-records')['total'],1)
        with self.assertRaises(HTTPError) as error:call('/api/task-records/preview',{'task_ids':['a']},auth=False)
        self.assertEqual(error.exception.code,403)
        preview=call('/api/task-records/preview',{'task_ids':['a']})
        result=call('/api/task-records/delete',{'token':preview['token']})
        self.assertEqual(result['deleted_tasks'],1)
        for method,path in [('GET','/api/task-records'),('POST','/api/task-records/preview'),('POST','/api/task-records/delete')]:
            self.assertEqual(desk.validate_request(path,method),path)
        for method,path in [('GET','/api/task-records/delete'),('POST','/api/task-records'),('POST','/api/task-records/../delete')]:
            with self.assertRaises(desk.PanelError):desk.validate_request(path,method)
