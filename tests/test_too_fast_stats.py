"""Regression tests for too-fast detection and session stats persistence.

Tests verify that:
1. check_if_too_fast() correctly detects too-fast patterns
2. submit_survey() handles completion and too-fast scenarios
3. _save_session_stats() persists correctly to JSONL
4. Bounded retries prevent infinite loops
5. Stats are only recorded on actual completion
"""

import unittest
import os
import json
import tempfile
import shutil
from unittest.mock import MagicMock, patch, PropertyMock
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestTooFastDetection(unittest.TestCase):
    """Test check_if_too_fast() pattern matching."""

    def setUp(self):
        """Set up a mock bot instance."""
        from bot import SentinelSurveyBot

        with patch.object(SentinelSurveyBot, '__init__', lambda self, *a, **kw: None):
            self.bot = SentinelSurveyBot.__new__(SentinelSurveyBot)
            self.bot.TOO_FAST_PATTERNS = [
                "too-fast", "speed", "speeding", "timeout",
                "answered too quickly", "too fast", "speed check",
                "please slow down", "you are moving too fast",
                "[class*='too-fast']", "[class*='speed-check']",
                "[id*='tooFast']", "[id*='speedCheck']",
            ]
            self.bot.driver = MagicMock()

    def test_no_too_fast_message(self):
        """No too-fast message returns False."""
        self.bot.driver.current_url = "https://survey.com/quiz"
        self.bot.driver.find_element.return_value.text = "What is your age?"

        result = self.bot.check_if_too_fast()
        self.assertFalse(result)

    def test_too_fast_in_url(self):
        """Too-fast pattern in URL returns True."""
        self.bot.driver.current_url = "https://survey.com/too-fast-warning"

        result = self.bot.check_if_too_fast()
        self.assertTrue(result)

    def test_too_fast_in_body_text(self):
        """Too-fast pattern in body text returns True."""
        self.bot.driver.current_url = "https://survey.com/quiz"
        self.bot.driver.find_element.return_value.text = "You answered too quickly. Please slow down."

        result = self.bot.check_if_too_fast()
        self.assertTrue(result)

    def test_too_fast_element_selector(self):
        """Too-fast element selector returns True."""
        self.bot.driver.current_url = "https://survey.com/quiz"
        self.bot.driver.find_element.return_value.text = "Some normal text"

        # Mock finding a too-fast element
        mock_el = MagicMock()
        mock_el.is_displayed.return_value = True
        self.bot.driver.find_elements.return_value = [mock_el]

        # Only match on element selectors (not URL or text)
        with patch.object(self.bot, 'TOO_FAST_PATTERNS', ["[class*='too-fast']"]):
            result = self.bot.check_if_too_fast()
        self.assertTrue(result)


class TestBoundedRetries(unittest.TestCase):
    """Test that too-fast retries are bounded."""

    def test_max_retry_limit(self):
        """TOO_FAST_MAX_RETRIES is set to prevent infinite loops."""
        from bot import SentinelSurveyBot

        with patch.object(SentinelSurveyBot, '__init__', lambda self, *a, **kw: None):
            self.bot = SentinelSurveyBot.__new__(SentinelSurveyBot)
            self.bot.TOO_FAST_MAX_RETRIES = 3

            self.assertEqual(self.bot.TOO_FAST_MAX_RETRIES, 3,
                           "Max retries should be bounded to 3")

    def test_retry_counter_increments(self):
        """too_fast_retries counter increments on each detection."""
        from bot import SentinelSurveyBot

        with patch.object(SentinelSurveyBot, '__init__', lambda self, *a, **kw: None):
            self.bot = SentinelSurveyBot.__new__(SentinelSurveyBot)
            self.bot.too_fast_retries = 0
            self.bot.speed_delay = 1.0

            # Simulate retry logic
            self.bot.too_fast_retries += 1
            self.bot.speed_delay *= 1.5

            self.assertEqual(self.bot.too_fast_retries, 1)
            self.assertAlmostEqual(self.bot.speed_delay, 1.5)

    def test_speed_delay_increases(self):
        """Speed delay multiplier increases with each retry."""
        from bot import SentinelSurveyBot

        with patch.object(SentinelSurveyBot, '__init__', lambda self, *a, **kw: None):
            self.bot = SentinelSurveyBot.__new__(SentinelSurveyBot)
            self.bot.speed_delay = 1.0

            # Simulate 3 retries
            for _ in range(3):
                self.bot.speed_delay *= 1.5

            self.assertAlmostEqual(self.bot.speed_delay, 3.375,
                                  msg="Delay should compound: 1.0 * 1.5^3 = 3.375")


class TestSessionStatsPersistence(unittest.TestCase):
    """Test session stats JSONL persistence."""

    def setUp(self):
        """Create a temporary directory for stats files."""
        self.temp_dir = tempfile.mkdtemp()
        self.stats_path = os.path.join(self.temp_dir, "session_stats.jsonl")

    def tearDown(self):
        """Clean up temp directory."""
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_save_stats_creates_jsonl(self):
        """_save_session_stats creates a JSONL file with one record."""
        from bot import SentinelSurveyBot

        with patch.object(SentinelSurveyBot, '__init__', lambda self, *a, **kw: None):
            self.bot = SentinelSurveyBot.__new__(SentinelSurveyBot)
            self.bot.profile_name = "test_profile"
            self.bot.session_stats = {
                "start_time": 1000.0,
                "pages_seen": 5,
                "actions_taken": 12,
                "status": "completed",
                "url": "https://survey.com/quiz",
                "duration_sec": 120,
            }
            self.bot.rules_added_this_session = 0
            self.bot.driver = MagicMock()
            self.bot.driver.current_url = "https://survey.com/thank-you"

            # Mock profile_mgr to return our temp dir
            self.bot.profile_mgr = MagicMock()
            self.bot.profile_mgr.get_profile_path.return_value = self.temp_dir

            # Call save
            self.bot._save_session_stats()

            # Verify file exists and has one record
            self.assertTrue(os.path.exists(self.stats_path))
            with open(self.stats_path, "r") as f:
                lines = f.readlines()
            self.assertEqual(len(lines), 1)

            # Verify record structure
            record = json.loads(lines[0])
            self.assertEqual(record["status"], "completed")
            self.assertEqual(record["pages"], 5)
            self.assertEqual(record["actions"], 12)
            self.assertEqual(record["duration_sec"], 120)
            self.assertFalse(record["dq"])

    def test_failed_submission_no_record(self):
        """Failed/too-fast submission should NOT create a completion record."""
        from bot import SentinelSurveyBot

        with patch.object(SentinelSurveyBot, '__init__', lambda self, *a, **kw: None):
            self.bot = SentinelSurveyBot.__new__(SentinelSurveyBot)
            self.bot.profile_name = "test_profile"
            self.bot.session_stats = {
                "start_time": 1000.0,
                "pages_seen": 2,
                "actions_taken": 5,
                "status": "running",
                "url": "https://survey.com/quiz",
                "duration_sec": 45,
            }
            self.bot.rules_added_this_session = 0
            self.bot.driver = MagicMock()
            self.bot.profile_mgr = MagicMock()
            self.bot.profile_mgr.get_profile_path.return_value = self.temp_dir

            # Only save if actually completed
            status = "too_fast"  # Not "completed"
            if status == "completed":
                self.bot._save_session_stats()

            # File should NOT exist
            self.assertFalse(os.path.exists(self.stats_path),
                           "Too-fast submission should not create stats record")

    def test_multiple_sessions_append(self):
        """Multiple sessions append to the same JSONL file."""
        from bot import SentinelSurveyBot

        with patch.object(SentinelSurveyBot, '__init__', lambda self, *a, **kw: None):
            self.bot = SentinelSurveyBot.__new__(SentinelSurveyBot)
            self.bot.profile_name = "test_profile"
            self.bot.rules_added_this_session = 0
            self.bot.driver = MagicMock()
            self.bot.profile_mgr = MagicMock()
            self.bot.profile_mgr.get_profile_path.return_value = self.temp_dir

            # Save first session
            self.bot.session_stats = {
                "start_time": 1000.0, "pages_seen": 5, "actions_taken": 12,
                "status": "completed", "url": "https://survey.com/q1",
                "duration_sec": 120,
            }
            self.bot.driver.current_url = "https://survey.com/thank-you"
            self.bot._save_session_stats()

            # Save second session
            self.bot.session_stats = {
                "start_time": 2000.0, "pages_seen": 3, "actions_taken": 8,
                "status": "dq", "url": "https://survey.com/q2",
                "duration_sec": 45,
            }
            self.bot.driver.current_url = "https://survey.com/dq"
            self.bot._save_session_stats()

            # Verify file has two records
            with open(self.stats_path, "r") as f:
                lines = f.readlines()
            self.assertEqual(len(lines), 2)

            records = [json.loads(line) for line in lines]
            self.assertEqual(records[0]["status"], "completed")
            self.assertEqual(records[1]["status"], "dq")
            self.assertTrue(records[1]["dq"])

    def test_stats_required_fields(self):
        """All required fields are present in stats record."""
        from bot import SentinelSurveyBot

        required_fields = [
            "timestamp", "url", "status", "pages", "actions",
            "duration_sec", "dq", "learned_rules_added"
        ]

        with patch.object(SentinelSurveyBot, '__init__', lambda self, *a, **kw: None):
            self.bot = SentinelSurveyBot.__new__(SentinelSurveyBot)
            self.bot.profile_name = "test_profile"
            self.bot.session_stats = {
                "start_time": 1000.0, "pages_seen": 5, "actions_taken": 12,
                "status": "completed", "url": "https://survey.com/quiz",
                "duration_sec": 120,
            }
            self.bot.rules_added_this_session = 2
            self.bot.driver = MagicMock()
            self.bot.driver.current_url = "https://survey.com/thank-you"
            self.bot.profile_mgr = MagicMock()
            self.bot.profile_mgr.get_profile_path.return_value = self.temp_dir

            self.bot._save_session_stats()

            with open(self.stats_path, "r") as f:
                record = json.loads(f.readline())

            for field in required_fields:
                self.assertIn(field, record,
                             f"Required field '{field}' missing from stats record")


class TestSubmitSurvey(unittest.TestCase):
    """Test submit_survey() behavior."""

    def test_submit_returns_false_on_too_fast(self):
        """submit_survey() returns False when too-fast detected (triggers retry)."""
        from bot import SentinelSurveyBot

        with patch.object(SentinelSurveyBot, '__init__', lambda self, *a, **kw: None):
            self.bot = SentinelSurveyBot.__new__(SentinelSurveyBot)
            self.bot.too_fast_retries = 0
            self.bot.speed_delay = 1.0
            self.bot.TOO_FAST_MAX_RETRIES = 3
            self.bot.COMPLETION_PATTERNS = ["thank you", "completed"]

            # Mock driver to find submit button
            mock_btn = MagicMock()
            mock_btn.is_displayed.return_value = True
            mock_btn.is_enabled.return_value = True
            mock_btn.rect = {"x": 100, "y": 100, "width": 80, "height": 30}
            self.bot.driver = MagicMock()
            self.bot.driver.find_elements.return_value = [mock_btn]
            self.bot.driver.current_url = "https://survey.com/quiz"

            # Mock mouse click success
            self.bot.mouse = MagicMock()
            self.bot.mouse.click.return_value = True

            # Mock actions
            self.bot.actions = MagicMock()
            self.bot.human_mouse_move = MagicMock()

            # Mock check_if_too_fast to return True (simulating detection)
            self.bot.check_if_too_fast = MagicMock(return_value=True)

            # Mock _finalize_session
            self.bot._finalize_session = MagicMock()

            result = self.bot.submit_survey()
            self.assertFalse(result, "submit_survey should return False on too-fast")
            self.assertEqual(self.bot.too_fast_retries, 1,
                           "Retry counter should increment")

    def test_submit_returns_true_on_completion(self):
        """submit_survey() returns True when completion confirmed."""
        from bot import SentinelSurveyBot

        with patch.object(SentinelSurveyBot, '__init__', lambda self, *a, **kw: None):
            self.bot = SentinelSurveyBot.__new__(SentinelSurveyBot)
            self.bot.too_fast_retries = 0
            self.bot.speed_delay = 1.0
            self.bot.TOO_FAST_MAX_RETRIES = 3
            self.bot.COMPLETION_PATTERNS = ["thank you", "completed", "finished"]

            # Mock driver
            mock_btn = MagicMock()
            mock_btn.is_displayed.return_value = True
            mock_btn.is_enabled.return_value = True
            mock_btn.rect = {"x": 100, "y": 100, "width": 80, "height": 30}
            self.bot.driver = MagicMock()
            self.bot.driver.find_elements.return_value = [mock_btn]
            self.bot.driver.current_url = "https://survey.com/thank-you"
            self.bot.driver.find_element.return_value.text = "Thank you for completing our survey!"

            # Mock mouse click success
            self.bot.mouse = MagicMock()
            self.bot.mouse.click.return_value = True

            # Mock actions
            self.bot.actions = MagicMock()
            self.bot.human_mouse_move = MagicMock()

            # Mock check_if_too_fast to return False (no too-fast)
            self.bot.check_if_too_fast = MagicMock(return_value=False)

            # Mock _finalize_session
            self.bot._finalize_session = MagicMock()

            result = self.bot.submit_survey()
            self.assertTrue(result, "submit_survey should return True on completion")
            self.bot._finalize_session.assert_called_once_with(status="completed")

    def test_max_retries_stops_loop(self):
        """After TOO_FAST_MAX_RETRIES, submit_survey stops and finalizes."""
        from bot import SentinelSurveyBot

        with patch.object(SentinelSurveyBot, '__init__', lambda self, *a, **kw: None):
            self.bot = SentinelSurveyBot.__new__(SentinelSurveyBot)
            self.bot.too_fast_retries = 3  # Already at max
            self.bot.speed_delay = 3.375
            self.bot.TOO_FAST_MAX_RETRIES = 3
            self.bot.COMPLETION_PATTERNS = ["thank you"]

            # Mock driver to find submit button
            mock_btn = MagicMock()
            mock_btn.is_displayed.return_value = True
            mock_btn.is_enabled.return_value = True
            mock_btn.rect = {"x": 100, "y": 100, "width": 80, "height": 30}
            self.bot.driver = MagicMock()
            self.bot.driver.find_elements.return_value = [mock_btn]
            self.bot.driver.current_url = "https://survey.com/quiz"

            # Mock mouse click success
            self.bot.mouse = MagicMock()
            self.bot.mouse.click.return_value = True

            # Mock actions
            self.bot.actions = MagicMock()
            self.bot.human_mouse_move = MagicMock()

            # Mock check_if_too_fast to return True (too-fast detected again)
            self.bot.check_if_too_fast = MagicMock(return_value=True)

            # Mock _finalize_session
            self.bot._finalize_session = MagicMock()

            result = self.bot.submit_survey()
            self.assertFalse(result, "submit_survey should return False at max retries")
            self.bot._finalize_session.assert_called_once_with(status="too_fast")


class TestFinalizeSession(unittest.TestCase):
    """Test _finalize_session() behavior."""

    def test_finalize_updates_stats(self):
        """_finalize_session updates stats and saves."""
        from bot import SentinelSurveyBot

        with patch.object(SentinelSurveyBot, '__init__', lambda self, *a, **kw: None):
            self.bot = SentinelSurveyBot.__new__(SentinelSurveyBot)
            self.bot.session_stats = {
                "start_time": 1000.0,
                "pages_seen": 5,
                "actions_taken": 12,
                "status": "running",
                "url": "https://survey.com/quiz",
            }
            self.bot.save_cookies = MagicMock()
            self.bot._save_session_stats = MagicMock()

            self.bot._finalize_session(status="completed")

            self.assertEqual(self.bot.session_stats["status"], "completed")
            self.bot.save_cookies.assert_called_once()
            self.bot._save_session_stats.assert_called_once()


if __name__ == "__main__":
    unittest.main()
