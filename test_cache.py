#!/usr/bin/env python3
"""
Unit tests for SISTRIX cache logic: cache_is_fresh + fetch_sistrix
"""

import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

# Patch paths before importing web_panel
_tmp = tempfile.mkdtemp()
_cache_dir = os.path.join(_tmp, "cache")
os.makedirs(_cache_dir)

import web_panel
web_panel.CACHE_DIR = __import__("pathlib").Path(_cache_dir)


def _make_cached(label="TEST", country="es", mode="weekly",
                 current_value=0.5, previous_value=0.4,
                 latest_date="2026-03-16", cached_hours_ago=6, num_points=10):
    """Helper: create and write a cache file with controlled timestamp."""
    cached_at = (datetime.now() - timedelta(hours=cached_hours_ago)).isoformat()
    dates = []
    history = []
    base_date = datetime.fromisoformat(latest_date)
    step = 1 if mode == "daily" else 7
    for i in range(num_points):
        d = base_date - timedelta(days=i * step)
        dates.append(d.strftime("%Y-%m-%d"))
        history.append(round(current_value - i * 0.01, 6))
    data = {
        "domain": "test.com",
        "label": label,
        "country": country,
        "mode": mode,
        "current_value": current_value,
        "previous_value": previous_value,
        "history": history,
        "dates": dates,
        "cached_at": cached_at,
    }
    # Write directly to bypass write_cache (which overrides cached_at with now())
    path = web_panel.get_cache_path(label, country, mode)
    with open(path, "w") as f:
        json.dump(data, f)
    return data


def _clear_cache():
    """Remove all cache files."""
    for f in web_panel.CACHE_DIR.iterdir():
        f.unlink()


def _api_response(entries):
    """Build a mock SISTRIX API JSON response."""
    return {"answer": [{"sichtbarkeitsindex": entries}]}


def _api_entries(values_dates):
    """Build API entries from [(value, date), ...]."""
    return [{"value": str(v), "date": d} for v, d in values_dates]


DOMAIN_CONFIG = {
    "domain": "test.com",
    "country": "es",
    "label": "TEST",
    "mode": "weekly",
    "type": "domain",
}


class TestCacheIsFresh(unittest.TestCase):

    def test_no_data(self):
        self.assertFalse(web_panel.cache_is_fresh(None, "weekly"))
        self.assertFalse(web_panel.cache_is_fresh({}, "weekly"))

    def test_no_cached_at(self):
        self.assertFalse(web_panel.cache_is_fresh({"dates": ["2026-03-16"]}, "weekly"))

    # --- Weekly Check 1: time-based (24h) ---

    def test_weekly_fresh_under_24h(self):
        data = {
            "cached_at": (datetime.now() - timedelta(hours=12)).isoformat(),
            "dates": ["2026-03-16"],
        }
        self.assertTrue(web_panel.cache_is_fresh(data, "weekly"))

    def test_weekly_stale_over_24h(self):
        data = {
            "cached_at": (datetime.now() - timedelta(hours=25)).isoformat(),
            "dates": ["2026-03-16"],
        }
        self.assertFalse(web_panel.cache_is_fresh(data, "weekly"))

    def test_weekly_stale_exactly_24h(self):
        data = {
            "cached_at": (datetime.now() - timedelta(hours=24)).isoformat(),
            "dates": ["2026-03-16"],
        }
        self.assertFalse(web_panel.cache_is_fresh(data, "weekly"))

    # --- Daily Check 1: time-based (6h) ---

    def test_daily_fresh_under_6h(self):
        data = {
            "cached_at": (datetime.now() - timedelta(hours=3)).isoformat(),
            "dates": [datetime.now().strftime("%Y-%m-%d")],
        }
        self.assertTrue(web_panel.cache_is_fresh(data, "daily"))

    def test_daily_stale_over_6h(self):
        data = {
            "cached_at": (datetime.now() - timedelta(hours=7)).isoformat(),
            "dates": [datetime.now().strftime("%Y-%m-%d")],
        }
        self.assertFalse(web_panel.cache_is_fresh(data, "daily"))

    # --- Check 2: data age ---

    def test_weekly_data_too_old(self):
        data = {
            "cached_at": (datetime.now() - timedelta(hours=1)).isoformat(),
            "dates": [(datetime.now() - timedelta(days=8)).strftime("%Y-%m-%d")],
        }
        self.assertFalse(web_panel.cache_is_fresh(data, "weekly"))

    def test_weekly_data_7_days_old_still_fresh(self):
        data = {
            "cached_at": (datetime.now() - timedelta(hours=1)).isoformat(),
            "dates": [(datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")],
        }
        self.assertTrue(web_panel.cache_is_fresh(data, "weekly"))

    def test_daily_data_too_old(self):
        data = {
            "cached_at": (datetime.now() - timedelta(hours=1)).isoformat(),
            "dates": [(datetime.now() - timedelta(days=4)).strftime("%Y-%m-%d")],
        }
        self.assertFalse(web_panel.cache_is_fresh(data, "daily"))

    # --- skip_time_check ---

    def test_skip_time_check_only_checks_data_age(self):
        """With skip_time_check, a 48h-old cache with recent data is still fresh."""
        data = {
            "cached_at": (datetime.now() - timedelta(hours=48)).isoformat(),
            "dates": [datetime.now().strftime("%Y-%m-%d")],
        }
        self.assertFalse(web_panel.cache_is_fresh(data, "weekly", skip_time_check=False))
        self.assertTrue(web_panel.cache_is_fresh(data, "weekly", skip_time_check=True))

    def test_skip_time_check_still_rejects_old_data(self):
        """With skip_time_check, old data is still rejected."""
        data = {
            "cached_at": (datetime.now() - timedelta(hours=48)).isoformat(),
            "dates": [(datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d")],
        }
        self.assertFalse(web_panel.cache_is_fresh(data, "weekly", skip_time_check=True))


class TestFetchSistrix(unittest.TestCase):

    def setUp(self):
        _clear_cache()

    def tearDown(self):
        _clear_cache()

    # --- Case 1: Cache fresh → return cache, 0 API calls ---

    @patch.object(web_panel, "http_requests")
    @patch.object(web_panel, "load_config", return_value={"sistrix_api_key": "KEY123"})
    def test_cache_fresh_no_api_call(self, mock_config, mock_http):
        _make_cached(cached_hours_ago=6, latest_date=datetime.now().strftime("%Y-%m-%d"))
        result = web_panel.fetch_sistrix(DOMAIN_CONFIG)
        self.assertTrue(result["_from_cache"])
        mock_http.get.assert_not_called()

    # --- Case 2: Cache >24h, API has no new data → quick check, 1 credit ---

    @patch.object(web_panel, "load_config", return_value={"sistrix_api_key": "KEY123"})
    def test_cache_stale_no_change_quick_check(self, mock_config):
        _make_cached(cached_hours_ago=30, current_value=0.69, latest_date="2026-03-16")
        api_resp = _api_response(_api_entries([(0.69, "2026-03-16")]))
        mock_resp = MagicMock()
        mock_resp.json.return_value = api_resp
        mock_resp.raise_for_status = MagicMock()

        with patch.object(web_panel.http_requests, "get", return_value=mock_resp) as mock_get:
            result = web_panel.fetch_sistrix(DOMAIN_CONFIG)
            # Should only call API once (quick check, no full fetch)
            self.assertEqual(mock_get.call_count, 1)
            # Should NOT have history=true in params
            call_params = mock_get.call_args[1].get("params", mock_get.call_args[0][0] if len(mock_get.call_args[0]) > 0 else {})
            actual_params = mock_get.call_args.kwargs.get("params", {})
            self.assertNotIn("history", actual_params)
            self.assertTrue(result["_from_cache"])
            self.assertIn("1 credit", result.get("_credits_note", ""))
            self.assertEqual(result["current_value"], 0.69)

    # --- Case 3: Cache >24h, new data → quick check + full fetch ---

    @patch.object(web_panel, "load_config", return_value={"sistrix_api_key": "KEY123"})
    def test_cache_stale_new_data_full_fetch(self, mock_config):
        _make_cached(cached_hours_ago=30, current_value=0.03, latest_date="2026-03-09")
        quick_resp = _api_response(_api_entries([(0.69, "2026-03-16")]))
        full_entries = _api_entries([
            (0.69, "2026-03-16"), (0.03, "2026-03-09"), (0.01, "2026-03-02"),
        ])
        full_resp = _api_response(full_entries)

        mock_resp_quick = MagicMock()
        mock_resp_quick.json.return_value = quick_resp
        mock_resp_quick.raise_for_status = MagicMock()

        mock_resp_full = MagicMock()
        mock_resp_full.json.return_value = full_resp
        mock_resp_full.raise_for_status = MagicMock()

        with patch.object(web_panel.http_requests, "get", side_effect=[mock_resp_quick, mock_resp_full]) as mock_get:
            result = web_panel.fetch_sistrix(DOMAIN_CONFIG)
            self.assertEqual(mock_get.call_count, 2)
            self.assertFalse(result["_from_cache"])
            self.assertEqual(result["current_value"], 0.69)
            self.assertEqual(result["previous_value"], 0.03)
            self.assertEqual(result["dates"][0], "2026-03-16")

    # --- Case 4: Bug original (cache 4 days, data 9 days) → detect + fetch ---

    @patch.object(web_panel, "load_config", return_value={"sistrix_api_key": "KEY123"})
    def test_bug_original_stale_cache_detects_new_data(self, mock_config):
        _make_cached(cached_hours_ago=96, current_value=0.03, latest_date="2026-03-09")
        quick_resp = _api_response(_api_entries([(0.69, "2026-03-16")]))
        full_resp = _api_response(_api_entries([
            (0.69, "2026-03-16"), (0.03, "2026-03-09"),
        ]))

        mock_q = MagicMock(); mock_q.json.return_value = quick_resp; mock_q.raise_for_status = MagicMock()
        mock_f = MagicMock(); mock_f.json.return_value = full_resp; mock_f.raise_for_status = MagicMock()

        with patch.object(web_panel.http_requests, "get", side_effect=[mock_q, mock_f]):
            result = web_panel.fetch_sistrix(DOMAIN_CONFIG)
            self.assertEqual(result["current_value"], 0.69)
            self.assertFalse(result["_from_cache"])

    # --- Case 5: force=True → skip cache, skip quick check, full fetch ---

    @patch.object(web_panel, "load_config", return_value={"sistrix_api_key": "KEY123"})
    def test_force_skips_cache_and_quick_check(self, mock_config):
        _make_cached(cached_hours_ago=1, current_value=0.69, latest_date="2026-03-16")
        full_resp = _api_response(_api_entries([(0.69, "2026-03-16"), (0.03, "2026-03-09")]))

        mock_r = MagicMock(); mock_r.json.return_value = full_resp; mock_r.raise_for_status = MagicMock()

        with patch.object(web_panel.http_requests, "get", return_value=mock_r) as mock_get:
            result = web_panel.fetch_sistrix(DOMAIN_CONFIG, force=True)
            # Only 1 call (full fetch, no quick check)
            self.assertEqual(mock_get.call_count, 1)
            # Must have history=true
            actual_params = mock_get.call_args.kwargs.get("params", {})
            self.assertEqual(actual_params.get("history"), "true")
            self.assertFalse(result["_from_cache"])

    # --- Case 6: refresh=True, data recent → return cache ---

    @patch.object(web_panel, "http_requests")
    @patch.object(web_panel, "load_config", return_value={"sistrix_api_key": "KEY123"})
    def test_refresh_with_recent_data_returns_cache(self, mock_config, mock_http):
        _make_cached(cached_hours_ago=30, latest_date=datetime.now().strftime("%Y-%m-%d"))
        result = web_panel.fetch_sistrix(DOMAIN_CONFIG, refresh=True)
        self.assertTrue(result["_from_cache"])
        mock_http.get.assert_not_called()

    # --- Case 7: refresh=True, data old (>7d) → fetch ---

    @patch.object(web_panel, "load_config", return_value={"sistrix_api_key": "KEY123"})
    def test_refresh_with_old_data_fetches(self, mock_config):
        old_date = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d")
        _make_cached(cached_hours_ago=30, current_value=0.01, latest_date=old_date)
        quick_resp = _api_response(_api_entries([(0.69, "2026-03-16")]))
        full_resp = _api_response(_api_entries([(0.69, "2026-03-16"), (0.03, "2026-03-09")]))

        mock_q = MagicMock(); mock_q.json.return_value = quick_resp; mock_q.raise_for_status = MagicMock()
        mock_f = MagicMock(); mock_f.json.return_value = full_resp; mock_f.raise_for_status = MagicMock()

        with patch.object(web_panel.http_requests, "get", side_effect=[mock_q, mock_f]):
            result = web_panel.fetch_sistrix(DOMAIN_CONFIG, refresh=True)
            self.assertFalse(result["_from_cache"])
            self.assertEqual(result["current_value"], 0.69)

    # --- Case 8: No cache (new domain) → full fetch ---

    @patch.object(web_panel, "load_config", return_value={"sistrix_api_key": "KEY123"})
    def test_no_cache_full_fetch(self, mock_config):
        full_resp = _api_response(_api_entries([(0.69, "2026-03-16"), (0.03, "2026-03-09")]))
        mock_r = MagicMock(); mock_r.json.return_value = full_resp; mock_r.raise_for_status = MagicMock()

        with patch.object(web_panel.http_requests, "get", return_value=mock_r) as mock_get:
            result = web_panel.fetch_sistrix(DOMAIN_CONFIG)
            # Only 1 call (full fetch, no quick check because no cache)
            self.assertEqual(mock_get.call_count, 1)
            actual_params = mock_get.call_args.kwargs.get("params", {})
            self.assertEqual(actual_params.get("history"), "true")
            self.assertFalse(result["_from_cache"])
            self.assertEqual(result["current_value"], 0.69)

    # --- Case 9: No API key → return cache ---

    def test_no_api_key_returns_cache(self):
        _make_cached(cached_hours_ago=100, latest_date="2026-03-01")
        result = web_panel.fetch_sistrix(DOMAIN_CONFIG, api_key="")
        self.assertTrue(result["_from_cache"])

    def test_no_api_key_no_cache_returns_none(self):
        result = web_panel.fetch_sistrix(DOMAIN_CONFIG, api_key="")
        self.assertIsNone(result)

    # --- Case 10: Daily fresh (<6h) → cache ---

    @patch.object(web_panel, "http_requests")
    @patch.object(web_panel, "load_config", return_value={"sistrix_api_key": "KEY123"})
    def test_daily_fresh_returns_cache(self, mock_config, mock_http):
        daily_config = {**DOMAIN_CONFIG, "mode": "daily"}
        _make_cached(mode="daily", cached_hours_ago=3, latest_date=datetime.now().strftime("%Y-%m-%d"))
        result = web_panel.fetch_sistrix(daily_config)
        self.assertTrue(result["_from_cache"])
        mock_http.get.assert_not_called()

    # --- Case 11: Daily stale (>6h), no change → quick check ---

    @patch.object(web_panel, "load_config", return_value={"sistrix_api_key": "KEY123"})
    def test_daily_stale_no_change(self, mock_config):
        daily_config = {**DOMAIN_CONFIG, "mode": "daily"}
        _make_cached(mode="daily", cached_hours_ago=8, current_value=1.5, latest_date="2026-03-17")
        api_resp = _api_response(_api_entries([(1.5, "2026-03-17")]))
        mock_r = MagicMock(); mock_r.json.return_value = api_resp; mock_r.raise_for_status = MagicMock()

        with patch.object(web_panel.http_requests, "get", return_value=mock_r) as mock_get:
            result = web_panel.fetch_sistrix(daily_config)
            self.assertEqual(mock_get.call_count, 1)
            self.assertTrue(result["_from_cache"])
            self.assertEqual(result["current_value"], 1.5)

    # --- Case 12: Daily stale (>6h), new data → full fetch ---

    @patch.object(web_panel, "load_config", return_value={"sistrix_api_key": "KEY123"})
    def test_daily_stale_new_data(self, mock_config):
        daily_config = {**DOMAIN_CONFIG, "mode": "daily"}
        _make_cached(mode="daily", cached_hours_ago=8, current_value=1.5, latest_date="2026-03-17")
        quick_resp = _api_response(_api_entries([(1.7, "2026-03-18")]))
        full_resp = _api_response(_api_entries([(1.7, "2026-03-18"), (1.5, "2026-03-17")]))

        mock_q = MagicMock(); mock_q.json.return_value = quick_resp; mock_q.raise_for_status = MagicMock()
        mock_f = MagicMock(); mock_f.json.return_value = full_resp; mock_f.raise_for_status = MagicMock()

        with patch.object(web_panel.http_requests, "get", side_effect=[mock_q, mock_f]):
            result = web_panel.fetch_sistrix(daily_config)
            self.assertFalse(result["_from_cache"])
            self.assertEqual(result["current_value"], 1.7)

    # --- Case 13: Same date, different value (SISTRIX correction) → full fetch ---

    @patch.object(web_panel, "load_config", return_value={"sistrix_api_key": "KEY123"})
    def test_same_date_different_value_triggers_full_fetch(self, mock_config):
        _make_cached(cached_hours_ago=30, current_value=0.50, latest_date="2026-03-16")
        quick_resp = _api_response(_api_entries([(0.69, "2026-03-16")]))
        full_resp = _api_response(_api_entries([(0.69, "2026-03-16"), (0.03, "2026-03-09")]))

        mock_q = MagicMock(); mock_q.json.return_value = quick_resp; mock_q.raise_for_status = MagicMock()
        mock_f = MagicMock(); mock_f.json.return_value = full_resp; mock_f.raise_for_status = MagicMock()

        with patch.object(web_panel.http_requests, "get", side_effect=[mock_q, mock_f]):
            result = web_panel.fetch_sistrix(DOMAIN_CONFIG)
            self.assertFalse(result["_from_cache"])
            self.assertEqual(result["current_value"], 0.69)

    # --- Case 14: API error → fallback to cache ---

    @patch.object(web_panel, "load_config", return_value={"sistrix_api_key": "KEY123"})
    def test_api_error_returns_cache(self, mock_config):
        _make_cached(cached_hours_ago=30, current_value=0.5, latest_date="2026-03-16")
        with patch.object(web_panel.http_requests, "get", side_effect=Exception("Connection timeout")):
            result = web_panel.fetch_sistrix(DOMAIN_CONFIG)
            self.assertTrue(result["_from_cache"])
            self.assertEqual(result["current_value"], 0.5)

    def test_api_error_no_cache_returns_none(self):
        with patch.object(web_panel.http_requests, "get", side_effect=Exception("Connection timeout")):
            result = web_panel.fetch_sistrix(DOMAIN_CONFIG, api_key="KEY123")
            self.assertIsNone(result)

    # --- Case 15: Quick check with empty entries → return cache ---

    @patch.object(web_panel, "load_config", return_value={"sistrix_api_key": "KEY123"})
    def test_quick_check_empty_entries_returns_cache(self, mock_config):
        _make_cached(cached_hours_ago=30, current_value=0.5, latest_date="2026-03-16")
        empty_resp = _api_response([])
        mock_r = MagicMock(); mock_r.json.return_value = empty_resp; mock_r.raise_for_status = MagicMock()

        with patch.object(web_panel.http_requests, "get", return_value=mock_r) as mock_get:
            result = web_panel.fetch_sistrix(DOMAIN_CONFIG)
            # Should return cache, NOT do a second full fetch
            self.assertEqual(mock_get.call_count, 1)
            self.assertTrue(result["_from_cache"])

    # --- Case 16: Float precision — round(0.6929, 6) must match ---

    @patch.object(web_panel, "load_config", return_value={"sistrix_api_key": "KEY123"})
    def test_float_precision_match(self, mock_config):
        _make_cached(cached_hours_ago=30, current_value=0.6929, latest_date="2026-03-16")
        api_resp = _api_response(_api_entries([(0.6929, "2026-03-16")]))
        mock_r = MagicMock(); mock_r.json.return_value = api_resp; mock_r.raise_for_status = MagicMock()

        with patch.object(web_panel.http_requests, "get", return_value=mock_r) as mock_get:
            result = web_panel.fetch_sistrix(DOMAIN_CONFIG)
            self.assertEqual(mock_get.call_count, 1)  # quick check only
            self.assertTrue(result["_from_cache"])

    # --- Case 17: write_cache does NOT mutate original dict ---

    def test_write_cache_no_mutation(self):
        data = {"label": "X", "country": "es", "mode": "weekly", "value": 1}
        original_keys = set(data.keys())
        web_panel.write_cache("X", "es", "weekly", data)
        self.assertEqual(set(data.keys()), original_keys)
        self.assertNotIn("cached_at", data)

    # --- Case 18: Cache timestamp renewed after quick check ---

    @patch.object(web_panel, "load_config", return_value={"sistrix_api_key": "KEY123"})
    def test_quick_check_renews_cache_timestamp(self, mock_config):
        _make_cached(cached_hours_ago=30, current_value=0.69, latest_date="2026-03-16")
        old_cache = web_panel.read_cache("TEST", "es", "weekly")
        old_ts = old_cache["cached_at"]

        api_resp = _api_response(_api_entries([(0.69, "2026-03-16")]))
        mock_r = MagicMock(); mock_r.json.return_value = api_resp; mock_r.raise_for_status = MagicMock()

        with patch.object(web_panel.http_requests, "get", return_value=mock_r):
            web_panel.fetch_sistrix(DOMAIN_CONFIG)

        new_cache = web_panel.read_cache("TEST", "es", "weekly")
        self.assertNotEqual(new_cache["cached_at"], old_ts)
        # New timestamp should be more recent
        self.assertGreater(
            datetime.fromisoformat(new_cache["cached_at"]),
            datetime.fromisoformat(old_ts)
        )


class TestWriteCache(unittest.TestCase):

    def setUp(self):
        _clear_cache()

    def tearDown(self):
        _clear_cache()

    def test_write_and_read(self):
        data = {"current_value": 1.5, "history": [1.5, 1.4]}
        web_panel.write_cache("A", "es", "weekly", data)
        result = web_panel.read_cache("A", "es", "weekly")
        self.assertIsNotNone(result)
        self.assertEqual(result["current_value"], 1.5)
        self.assertIn("cached_at", result)

    def test_read_nonexistent(self):
        self.assertIsNone(web_panel.read_cache("NOPE", "xx", "weekly"))


if __name__ == "__main__":
    unittest.main()
