from datetime import datetime
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import deepseek_billing as billing


class PricingTests(unittest.TestCase):
    MODEL = {"model": "deepseek-flash", "provider": "deepseek", "api_base": "https://api.deepseek.com"}

    def test_peak_boundaries_in_beijing_time(self):
        for time, expected in (("08:59:59", "off_peak"), ("09:00:00", "peak"),
            ("11:59:59", "peak"), ("12:00:00", "off_peak"), ("13:59:59", "off_peak"),
            ("14:00:00", "peak"), ("17:59:59", "peak"), ("18:00:00", "off_peak")):
            with self.subTest(time=time):
                moment = datetime.fromisoformat("2026-10-13T" + time + "+08:00")
                self.assertEqual(billing.period(moment), expected)
        self.assertEqual(billing.period(datetime.fromisoformat("2026-10-13T06:00:00+00:00")), "peak")

    def test_holidays_and_makeup_weekends_are_off_peak(self):
        for day in ("2026-10-01", "2026-10-07", "2026-10-10", "2026-02-16", "2026-02-28", "2026-05-05"):
            with self.subTest(day=day):
                self.assertEqual(billing.period(datetime.fromisoformat(day + "T10:00:00+08:00")), "off_peak")

    def test_next_switch_and_off_peak_price(self):
        morning = billing.pricing(self.MODEL, datetime.fromisoformat("2026-10-13T11:00:00+08:00"))
        self.assertEqual(morning["next_change"]["at"], "2026-10-13T12:00:00+08:00")
        self.assertEqual(morning["next_change"]["in_seconds"], 3600)
        self.assertEqual(morning["rates"]["output"], "8")
        valley = billing.pricing(self.MODEL, datetime.fromisoformat("2026-10-13T12:30:00+08:00"))
        self.assertEqual(valley["rates"]["output"], "4")
        weekend = billing.pricing(self.MODEL, datetime.fromisoformat("2026-10-10T10:00:00+08:00"))
        self.assertEqual(weekend["next_change"]["at"], "2026-10-12T09:00:00+08:00")

    def test_unknown_future_calendar_and_third_party(self):
        self.assertEqual(billing.period(datetime.fromisoformat("2027-01-04T10:00:00+08:00")), "unknown")
        third_party = billing.pricing({**self.MODEL, "api_base": "https://example.org/v1"})
        self.assertEqual(third_party["period"], "third_party")
        self.assertIsNone(third_party["rates"])
        self.assertIsNone(third_party["top_up_url"])

    def test_alias_and_unknown_model_prices(self):
        moment = datetime.fromisoformat("2026-10-13T14:00:00+08:00")
        alias = billing.pricing({**self.MODEL, "model": "deepseek-v4-flash"}, moment)
        self.assertEqual(alias["rates"]["cache_miss_input"], "2")
        unknown = billing.pricing({**self.MODEL, "model": "unknown-model"}, moment)
        self.assertIsNone(unknown["rates"])
        self.assertEqual(unknown["period"], "peak")


class BalanceTests(unittest.TestCase):
    def test_currency_and_decimal_precision(self):
        data = {"is_available": True, "balance_infos": [{"currency": "CNY", "total_balance": "12.340001",
            "granted_balance": "0.34", "topped_up_balance": "12.000001"}]}
        self.assertEqual(billing.normalize_balances(data), data)
        with self.assertRaises(ValueError):
            billing.normalize_balances({"is_available": True, "balance_infos": [{"currency": "CNY", "total_balance": "NaN"}]})

    def test_refresh_failure_preserves_stale_balance(self):
        previous = {"identity": "test", "balance_infos": [{"currency": "CNY", "total_balance": "3.00"}],
                    "is_available": True, "updated_at": "previous"}
        with patch.dict(billing.CACHE, {"test": previous}, clear=True), \
             patch.object(billing.registry, "get_model", return_value={"api_key": "secret-not-for-client"}), \
             patch.object(billing, "read_balance", side_effect=OSError("secret-not-for-client")):
            billing.fetch_balance("test", "test")
            result = billing.CACHE["test"]
            self.assertEqual(result["status"], "error")
            self.assertTrue(result["stale"])
            self.assertEqual(result["balance_infos"], previous["balance_infos"])
            self.assertNotIn("secret-not-for-client", json.dumps(result))

    def test_missing_key_and_third_party_do_not_make_network_requests(self):
        with patch.object(billing, "read_balance") as network:
            missing = billing.balance_state({"api_base": "https://api.deepseek.com", "api_key_set": False})
            other = billing.balance_state({"api_base": "https://example.org/v1", "api_key_set": True})
            self.assertEqual(missing["status"], "missing_key")
            self.assertEqual(other["status"], "unsupported")
            network.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
