from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import install_workflow_rules as rules


class WorkflowGuideTests(unittest.TestCase):
    def test_core_is_compact_and_enforces_boundaries_before_on_demand_reads(self):
        text = rules.block()
        # Bound paths vary with the installation directory; measure the rule
        # content independently of an author's or CI runner's path length.
        self.assertLess(len(text.replace(str(rules.ROOT), '@ROOT@').encode('utf-8')), 7500)
        for required in ('--status', 'enabled=false', 'paid_allowed', 'enforce_paid_budget=false',
                         'enforce_model_call_limit=false', 'timeout=0', 'independent evidence for every criterion',
                         'Self-reports are claims', '--final-accept', '--wait-task <original-id>',
                         'same project/goal', 'Recovery_pending', 'exact model/executor/preset/version',
                         'Home/local never gets a child', 'not an OS sandbox', 'Never read/expose secrets',
                         'trusted calling project', 'Never initialize Git', 'Read only the operation-specific guide'):
            self.assertIn(required, text)
        self.assertNotIn('@ROOT@', text)
        self.assertIn('collaboration_status.py --project', text)
        self.assertIn('show `user_signal.text`', text)
        self.assertIn('errors mean not ready', text)

    def test_incomplete_markers_preserve_original(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'AGENTS.md'
            original = 'unmanaged\n' + rules.START + '\nbroken'
            path.write_text(original, encoding='utf-8')
            with self.assertRaises(ValueError):
                rules.install(directory)
            self.assertEqual(original, path.read_text(encoding='utf-8'))

    def test_install_preserves_both_unmanaged_sides_and_override_priority(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            primary = home / 'AGENTS.md'
            primary.write_text('primary stays', encoding='utf-8')
            override = home / 'AGENTS.override.md'
            override.write_text('before\n' + rules.START + '\nold\n' + rules.END + '\nafter\n', encoding='utf-8')
            self.assertEqual(rules.install(home), override)
            first = override.read_text(encoding='utf-8')
            self.assertTrue(first.startswith('before\n'))
            self.assertTrue(first.endswith('\nafter\n'))
            rules.install(home)
            self.assertEqual(first, override.read_text(encoding='utf-8'))
            self.assertEqual('primary stays', primary.read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
