from pathlib import Path
import unittest

from contextswarm_mini.config import load_config


ROOT = Path(__file__).resolve().parents[1]


class RevalidationManifestTests(unittest.TestCase):
    def test_parallel_matches_runtime_with_explicit_baseline_treatment(self):
        reference = load_config(ROOT / 'configs/figure4_formal_6datasets/matholympiadbench/repeat1/uniform_refill.toml', ROOT)
        parallel = load_config(ROOT / 'configs/revalidation_matholympiadbench/parallel.toml', ROOT)
        self.assertEqual((parallel.mode, parallel.communication), ('parallel', 'none'))
        self.assertFalse(parallel.selection.enabled)
        self.assertEqual((parallel.initial_agents_per_task, parallel.episodes_per_task), (1, 1))
        expected = reference.public_dict()
        actual = parallel.public_dict()
        for key in ('name', 'mode', 'communication', 'selection', 'figure4_phase',
                    'initial_agents_per_task', 'episodes_per_task', 'max_attempts_per_task'):
            expected.pop(key)
            actual.pop(key)
        expected['allocation'].pop('policy')
        actual['allocation'].pop('policy')
        self.assertEqual(expected, actual)

    def test_smokes_keep_distinct_contracts_and_separate_output(self):
        for family, selection, helpers in [('issue38', True, False), ('issue39', True, True), ('parallel', False, True)]:
            with self.subTest(family=family):
                config = load_config(ROOT / f'configs/revalidation_matholympiadbench/smoke_{family}.toml', ROOT)
                self.assertEqual(config.time_limit_seconds, 180)
                self.assertEqual(config.pi_timeout_seconds, 180)
                self.assertEqual(config.max_parallel, 24)
                self.assertEqual(config.lean_max_concurrent_evaluations, 4)
                self.assertEqual(config.seed, 1729)
                self.assertEqual(config.selection.enabled, selection)
                self.assertEqual(config.formal_tools_enabled, helpers)
                self.assertIn('maintenance_smoke', str(config.output_root))
