# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import json
import os
import sys
import unittest

sys.path.append(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)
sys.path.append(
    os.path.dirname(
        os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        )
    )
)

from adk_tokenomics_plugin.cli_web import (
    TokenomicsWebHandler,
    get_models_config_path,
)
from adk_tokenomics_plugin.core.config import (
    validate_bq_identifier,
    validate_bq_project_id,
)


class TestAdkFinopsNative(unittest.TestCase):
    def setUp(self):
        self._saved_class_state = {
            "project_id": TokenomicsWebHandler.project_id,
            "dataset_id": TokenomicsWebHandler.dataset_id,
            "table_id": TokenomicsWebHandler.table_id,
            "view_id": TokenomicsWebHandler.view_id,
            "traces_path": TokenomicsWebHandler.traces_path,
            "pricing": TokenomicsWebHandler.pricing,
            "ACTIVE_BQ_CONFIG": dict(
                TokenomicsWebHandler.ACTIVE_BQ_CONFIG or {}
            ),
        }
        self.handler = object.__new__(TokenomicsWebHandler)

    def tearDown(self):
        TokenomicsWebHandler.project_id = self._saved_class_state["project_id"]
        TokenomicsWebHandler.dataset_id = self._saved_class_state["dataset_id"]
        TokenomicsWebHandler.table_id = self._saved_class_state["table_id"]
        TokenomicsWebHandler.view_id = self._saved_class_state["view_id"]
        TokenomicsWebHandler.traces_path = self._saved_class_state[
            "traces_path"
        ]
        TokenomicsWebHandler.pricing = self._saved_class_state["pricing"]
        TokenomicsWebHandler.ACTIVE_BQ_CONFIG = dict(
            self._saved_class_state["ACTIVE_BQ_CONFIG"]
        )

    def test_cost_calculation_rates(self):
        with open(get_models_config_path(), encoding="utf-8") as f:
            models_cfg = json.load(f)

        # Gemini 3.8 Flash: 10k * $0.75/1M ($0.0075) + 40k * $0.075/1M ($0.0030) + 1.2k * $3.75/1M ($0.0045) = $0.0150
        res = self.handler._calc_model_cost(
            model_name="gemini-3.8-flash",
            input_tokens=10000,
            cached_tokens=40000,
            output_tokens=1000,
            thinking_tokens=200,
            models_config=models_cfg,
        )
        self.assertEqual(res["input_rate"], 0.75)
        self.assertEqual(res["cached_rate"], 0.075)
        self.assertEqual(res["output_rate"], 3.75)
        self.assertAlmostEqual(res["input_cost"], 0.0075, places=8)
        self.assertAlmostEqual(res["cached_cost"], 0.0030, places=8)
        self.assertAlmostEqual(res["output_cost"], 0.0045, places=8)
        self.assertAlmostEqual(res["total_cost"], 0.0150, places=8)
        self.assertAlmostEqual(res["savings_dollars"], 0.0270, places=8)
        self.assertAlmostEqual(
            res["savings_pct"], (0.0270 / 0.0420) * 100.0, places=4
        )

    def test_bq_config_getter_setter_and_state_restoration(self):
        updated = self.handler.set_bq_config(
            {
                "project_id": "test-project",
                "dataset_id": "test_ds",
                "table_id": "test_events",
                "view_id": "test_v_llm",
            }
        )
        self.assertEqual(updated["project_id"], "test-project")
        self.assertEqual(updated["dataset_id"], "test_ds")
        self.assertEqual(updated["table_id"], "test_events")
        self.assertEqual(updated["view_id"], "test_v_llm")

        fetched = self.handler.get_bq_config()
        self.assertEqual(fetched["project_id"], "test-project")
        self.assertEqual(fetched["dataset_id"], "test_ds")
        self.assertEqual(fetched["table_id"], "test_events")
        self.assertEqual(fetched["view_id"], "test_v_llm")

        TokenomicsWebHandler.set_server_context(
            project_id="ctx-project-01",
            dataset_id="ctx_ds",
            table_id="ctx_events",
            view_id="ctx_view",
        )
        fresh_handler = object.__new__(TokenomicsWebHandler)
        ctx_cfg = fresh_handler.get_bq_config()
        self.assertEqual(ctx_cfg["project_id"], "ctx-project-01")
        self.assertEqual(ctx_cfg["dataset_id"], "ctx_ds")
        self.assertEqual(ctx_cfg["table_id"], "ctx_events")
        self.assertEqual(ctx_cfg["view_id"], "ctx_view")

    def test_bq_identifier_and_project_validation(self):
        self.assertEqual(
            validate_bq_project_id("  my-gcp-project-01  "), "my-gcp-project-01"
        )
        self.assertEqual(
            validate_bq_project_id("example.com:domain-proj_1"),
            "example.com:domain-proj_1",
        )
        self.assertEqual(
            validate_bq_identifier("  bq_adk_ds_2026 ", "dataset_id"),
            "bq_adk_ds_2026",
        )
        self.assertEqual(
            validate_bq_identifier("123_starts_with_digit", "dataset_id"),
            "123_starts_with_digit",
        )

        for bad_proj in (
            "",
            "   ",
            "proj; DROP TABLE x",
            "-leading-hyphen",
            "../traversal",
            None,
            123,
        ):
            with self.assertRaises(ValueError):
                validate_bq_project_id(bad_proj)  # type: ignore[arg-type]

        self.assertEqual(
            validate_bq_identifier("123_starts_with_digit", "table_id"),
            "123_starts_with_digit",
        )
        self.assertEqual(
            validate_bq_identifier("table-with-dash", "table_id"),
            "table-with-dash",
        )

        for bad_ds in ("dataset-with-dash", "bad;drop"):
            with self.assertRaises(ValueError):
                validate_bq_identifier(bad_ds, "dataset_id")

        for bad_ident in (
            "",
            "   ",
            "-leading-dash",
            "tbl--comment",
            "ds.table",
            "a`;--",
            None,
        ):
            with self.assertRaises(ValueError):
                validate_bq_identifier(bad_ident, "table_id")  # type: ignore[arg-type]

    def test_empty_finops_summary_structure(self):
        empty = self.handler._empty_finops_summary("Test error")
        self.assertEqual(empty["status"], "error")
        self.assertEqual(empty["message"], "Test error")
        self.assertIn("kpis", empty)
        self.assertEqual(empty["kpis"]["total_spend"], 0.0)
        self.assertIn("provider_breakdown", empty)
        self.assertIn("Google Gemini", empty["provider_breakdown"])
        self.assertIn("Anthropic Claude", empty["provider_breakdown"])
        self.assertIn("OpenAI", empty["provider_breakdown"])
        self.assertIn("Open-Weight / MaaS", empty["provider_breakdown"])


if __name__ == "__main__":
    unittest.main()
