from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from contextswarm_mini.config import ConfigError, load_config


ROOT = Path(__file__).resolve().parents[1]
SELECTORS = (
    "random",
    "recency",
    "bm25_mmr",
    "smoothed_popularity",
    "feedback_diversity",
    "no_interaction_feedback",
    "unnormalized_feedback",
    "nustigmergy",
)


def _selection_table(selector_name: str = "random") -> str:
    return f"""
[selection]
enabled = true
selector_name = "{selector_name}"
selector_version = "figure3_v1"
visibility = "project_shared"
trace_slot_limit = 3
context_token_budget = 4096
tokenizer = "unicode_word_v1"
seed = 17
tie_break = "trace_id_asc"
direct_messages = false
candidate_transfer = false

[selection.policy_params]
sample_without_replacement = true
weights = {{ interaction = 1.25, evidence = 0.75 }}
kinds = ["useful", "not_useful"]
[experiment]
seed = 17
"""


class SelectionConfigTests(unittest.TestCase):
    def _load(self, body: str):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "selection.toml"
            path.write_text(
                f'extends = ["{ROOT / "configs" / "smoke.toml"}"]\n{body}',
                encoding="utf-8",
            )
            return load_config(path, ROOT)

    def test_legacy_manifests_leave_selection_disabled(self) -> None:
        config = load_config("configs/smoke.toml", ROOT)

        self.assertFalse(config.selection.enabled)
        self.assertEqual(config.selection.selector_name, "")
        self.assertEqual(config.selection.policy_params, {})
        self.assertEqual(config.selection.trace_slot_limit, 0)
        self.assertEqual(config.selection.context_token_budget, 0)
        self.assertFalse(config.selection.direct_messages)
        self.assertFalse(config.selection.candidate_transfer)
        self.assertEqual(
            config.public_dict()["selection"], config.selection.public_dict()
        )

    def test_all_registered_selector_names_are_accepted(self) -> None:
        for selector_name in SELECTORS:
            with self.subTest(selector_name=selector_name):
                config = self._load(_selection_table(selector_name))
                self.assertTrue(config.selection.enabled)
                self.assertTrue(config.selection.identity_frozen)
                self.assertEqual(config.selection.selector_name, selector_name)

    def test_enabled_selection_requires_every_common_and_isolation_field(self) -> None:
        table = _selection_table()
        required_lines = (
            'selector_name = "random"',
            'selector_version = "figure3_v1"',
            'visibility = "project_shared"',
            "trace_slot_limit = 3",
            "context_token_budget = 4096",
            'tokenizer = "unicode_word_v1"',
            "seed = 17",
            'tie_break = "trace_id_asc"',
            "direct_messages = false",
            "candidate_transfer = false",
        )
        for line in required_lines:
            with self.subTest(missing=line):
                with self.assertRaisesRegex(ConfigError, "required when enabled"):
                    self._load(table.replace(line, "", 1))
        with self.assertRaisesRegex(ConfigError, "required when enabled"):
            self._load(table.split("\n[selection.policy_params]\n", 1)[0] + "\n")

    def test_enabled_selection_rejects_non_isolated_or_non_shared_arms(self) -> None:
        invalid = (
            ("direct_messages = false", "direct_messages = true"),
            ("candidate_transfer = false", "candidate_transfer = true"),
            ('visibility = "project_shared"', 'visibility = "task_local"'),
            ("trace_slot_limit = 3", "trace_slot_limit = 0"),
            ("context_token_budget = 4096", "context_token_budget = -1"),
        )
        for old, new in invalid:
            with self.subTest(setting=new):
                with self.assertRaises(ConfigError):
                    self._load(_selection_table().replace(old, new))

    def test_enabled_selection_requires_cps_communication_path(self) -> None:
        cases = (
            ('mode = "parallel"\ncommunication = "none"',
             "requires experiment.mode = cps"),
            ('communication = "none"',
             "requires experiment.communication = blackboard"),
            ('communication = "direct"',
             "requires experiment.communication = blackboard"),
            ('communication = "hybrid"',
             "requires experiment.communication = blackboard"),
            ('communication = "simple"',
             "requires experiment.communication = blackboard"),
        )
        for experiment_override, message in cases:
            with self.subTest(setting=experiment_override):
                with self.assertRaisesRegex(ConfigError, message):
                    self._load(
                        _selection_table().replace(
                            "[experiment]\nseed = 17",
                            "[experiment]\nseed = 17\n" + experiment_override,
                        )
                    )

    def test_enabled_selection_requires_one_paired_seed(self) -> None:
        config = self._load(_selection_table())
        self.assertEqual(config.selection.seed, config.seed)
        for old, new in (
            ("[experiment]\nseed = 17", "[experiment]\nseed = 18"),
            ("tokenizer = \"unicode_word_v1\"\nseed = 17",
             "tokenizer = \"unicode_word_v1\"\nseed = 18"),
        ):
            with self.subTest(new=new):
                with self.assertRaisesRegex(
                    ConfigError,
                    r"selection\.seed == experiment\.seed",
                ):
                    self._load(_selection_table().replace(old, new))

    def test_selection_booleans_and_seed_are_strictly_typed(self) -> None:
        invalid = (
            ("enabled = true", 'enabled = "true"'),
            ("direct_messages = false", 'direct_messages = "false"'),
            ("candidate_transfer = false", "candidate_transfer = 0"),
            ("seed = 17", "seed = true"),
            ("trace_slot_limit = 3", "trace_slot_limit = 3.0"),
        )
        for old, new in invalid:
            with self.subTest(setting=new):
                with self.assertRaises(ConfigError):
                    self._load(_selection_table().replace(old, new))

    def test_unknown_fields_and_nonfinite_policy_values_are_rejected(self) -> None:
        with self.assertRaisesRegex(ConfigError, "unknown selection fields"):
            self._load(_selection_table().replace("enabled = true", "enabled = true\nmagic = 1"))
        with self.assertRaisesRegex(ConfigError, "must be finite"):
            self._load(
                _selection_table().replace(
                    "sample_without_replacement = true",
                    "sample_without_replacement = true\ninvalid = nan",
                )
            )

    def test_public_dict_and_identity_use_canonical_full_configuration(self) -> None:
        config = self._load(_selection_table("nustigmergy"))
        selection = config.selection
        canonical = json.dumps(
            selection.hash_inputs(),
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

        self.assertEqual(
            selection.selection_config_id, hashlib.sha256(canonical).hexdigest()
        )
        public = selection.public_dict()
        self.assertEqual(public["selection_config_id"], selection.selection_config_id)
        self.assertEqual(public["policy_params"]["weights"]["interaction"], 1.25)

    def test_comparison_hash_inputs_exclude_only_policy_identity(self) -> None:
        random_config = self._load(_selection_table("random")).selection
        nu_config = self._load(
            _selection_table("nustigmergy").replace(
                "sample_without_replacement = true",
                "sample_without_replacement = false",
            )
        ).selection

        self.assertNotEqual(
            random_config.selection_config_id, nu_config.selection_config_id
        )
        self.assertEqual(
            random_config.comparison_hash_inputs(),
            nu_config.comparison_hash_inputs(),
        )
        self.assertNotIn("selector_name", random_config.comparison_hash_inputs())
        self.assertNotIn("policy_params", random_config.comparison_hash_inputs())

    def test_explicit_formal_figure4_requires_frozen_enabled_selector(self) -> None:
        with self.assertRaisesRegex(ConfigError, "one of the four registered"):
            self._load("[experiment]\nfigure4_phase = \"formal\"\n")

        with self.assertRaisesRegex(ConfigError, "complete frozen enabled selector"):
            self._load(
                "[experiment]\nfigure4_phase = \"formal\"\n"
                "\n[allocation]\npolicy = \"trace_state\"\n"
            )

        config = self._load(
            _selection_table("nustigmergy").replace(
                "candidate_transfer = false", "candidate_transfer = true"
            )
            .replace(
                "[experiment]\nseed = 17",
                "[experiment]\nseed = 17\nfigure4_phase = \"formal\"",
            )
            + "\n[allocation]\npolicy = \"trace_state\"\n"
        )
        self.assertEqual(config.figure4_phase, "formal")
        self.assertTrue(config.selection.identity_frozen)

    def test_formal_figure4_separates_selector_and_experiment_seed(self) -> None:
        # Figure 4 freezes the selector identity seed while a paired repeat
        # may use a distinct experiment/stochastic seed.  Changing only the
        # latter must not change the selector configuration hash.
        common = (
            _selection_table("recency")
            .replace(
                "[selection.policy_params]\n"
                "sample_without_replacement = true\n"
                "weights = { interaction = 1.25, evidence = 0.75 }\n"
                "kinds = [\"useful\", \"not_useful\"]",
                "[selection.policy_params]\n"
                "primary_sort = \"commit_seq_desc\"",
            )
            .replace(
                "[experiment]\nseed = 17",
                "[experiment]\nseed = SEED_PLACEHOLDER\nfigure4_phase = \"formal\"",
            )
            .replace("candidate_transfer = false", "candidate_transfer = true")
            + "\n[allocation]\npolicy = \"task_state\"\n"
        )
        first = self._load(common.replace("SEED_PLACEHOLDER", "1730"))
        second = self._load(common.replace("SEED_PLACEHOLDER", "1731"))
        self.assertEqual(first.figure4_phase, "formal")
        self.assertEqual(first.selection.seed, 17)
        self.assertEqual(first.seed, 1730)
        self.assertEqual(second.seed, 1731)
        self.assertEqual(
            first.selection.selection_config_id,
            second.selection.selection_config_id,
        )

    def test_nonformal_selection_still_rejects_seed_separation(self) -> None:
        with self.assertRaisesRegex(
            ConfigError,
            r"enabled selection requires selection\.seed == experiment\.seed",
        ):
            self._load(
                _selection_table("recency").replace(
                    "[experiment]\nseed = 17",
                    "[experiment]\nseed = 1730",
                )
            )

    def test_explicit_development_figure4_requires_disabled_selector(self) -> None:
        with self.assertRaisesRegex(ConfigError, "selection.enabled = false"):
            self._load(
                _selection_table().replace(
                    "[experiment]\nseed = 17",
                    "[experiment]\nseed = 17\nfigure4_phase = \"development\"",
                )
                + "\n[allocation]\npolicy = \"trace_state\"\n"
            )

    def test_figure4_phase_rejects_legacy_allocation_policy(self) -> None:
        with self.assertRaisesRegex(ConfigError, "one of the four registered"):
            self._load(
                "[experiment]\nfigure4_phase = \"development\"\n"
                "\n[allocation]\npolicy = \"formula\"\n"
            )


if __name__ == "__main__":
    unittest.main()
