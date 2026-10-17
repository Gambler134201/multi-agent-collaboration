"""Main review regressions for damaged records that previously claimed acceptance."""
import test_workflow_assist as fixtures
import delegation_policy as policy
import model_registry as registry
import workflow_assist as wa


class CorruptAcceptanceReviewTests(fixtures.Base):
    def test_numeric_criteria_is_explicitly_untrusted_without_crashing(self):
        run = self.task('review-number-criteria')
        self.accept(run)
        record = policy.load(run)
        record['criteria'] = 7
        policy.save(run, record)
        packet = wa.delivery_packet(run.name)
        self.assertFalse(packet['acceptance']['independently_accepted'])
        self.assertTrue(packet['errors'])

    def test_numeric_evidence_is_explicitly_untrusted_without_crashing(self):
        run = self.task('review-number-evidence')
        self.accept(run)
        record = policy.load(run)
        record['criteria'][0]['evidence'] = 7
        policy.save(run, record)
        packet = wa.delivery_packet(run.name)
        self.assertFalse(packet['acceptance']['independently_accepted'])
        self.assertTrue(packet['errors'])

    def test_boolean_false_is_not_a_successful_process_exit_status(self):
        run = self.task('review-bool-exit')
        self.accept(run)
        record = policy.load(run)
        record['criteria'][0]['evidence'][0]['exit_code'] = False
        policy.save(run, record)
        self.assertTrue(policy.acceptance_problems(record))
        self.assertFalse(wa.delivery_packet(run.name)['acceptance']['independently_accepted'])

    def test_missing_record_identity_cannot_establish_acceptance(self):
        for target in ('task', 'review'):
            with self.subTest(target=target):
                run = self.task('review-missing-id-' + target)
                self.accept(run)
                path = run / (target + '.json')
                record = registry.read_json(path)
                record.pop('id' if target == 'task' else 'task_id')
                registry.write_json(path, record)
                packet = wa.delivery_packet(run.name)
                self.assertFalse(packet['acceptance']['independently_accepted'])
                self.assertTrue(packet['errors'])
