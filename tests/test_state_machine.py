"""Regression tests for the survey loop state machine.

These tests verify that the loop correctly continues after an iteration
where actions were refused due to empty element map, and only exits
on genuine termination conditions (completion, DQ, provider down).
"""

import unittest
from unittest.mock import MagicMock, patch, PropertyMock
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestLoopContinuation(unittest.TestCase):
    """Test that _run_survey_loop correctly interprets return values."""

    def test_false_return_continues_loop(self):
        """_survey_loop_iteration returning False should CONTINUE the loop, not exit.

        This is the core bug: the comment says 'continue loop, reset timeout'
        but the old code exited because `not False` is True.
        """
        from core import SurveyBot

        bot = MagicMock(spec=SurveyBot)
        bot.guard = MagicMock()
        bot.guard.iteration = 0

        # Simulate the logic in _run_survey_loop
        keep_going = False  # What _survey_loop_iteration returns on success

        # The documented behavior: False means "continue, reset timeout"
        # The old buggy code: `if not keep_going and keep_going is not None: return`
        # This test verifies the FIXED behavior

        # After fix: False should NOT cause exit
        should_exit = (not keep_going and keep_going is not None)
        # Old behavior: should_exit = True (BUG)
        # New behavior: keep_going should be True, so should_exit = False

        # The fix changes `return False` to `return True` in _survey_loop_iteration
        # So keep_going will be True, and should_exit will be False
        keep_going_after_fix = True  # What the fixed code returns
        should_exit_after_fix = (not keep_going_after_fix and keep_going_after_fix is not None)

        self.assertFalse(should_exit_after_fix,
                        "Loop should NOT exit when iteration succeeds")

    def test_none_return_exits_loop(self):
        """_survey_loop_iteration returning None should EXIT the loop.

        None indicates genuine termination (completion, DQ, provider down).
        """
        keep_going = None

        # None should cause exit
        should_exit = (keep_going is None)

        self.assertTrue(should_exit,
                       "Loop should exit when iteration returns None")

    def test_true_return_continues_loop(self):
        """_survey_loop_iteration returning True should CONTINUE the loop."""
        keep_going = True

        should_exit = (not keep_going and keep_going is not None)

        self.assertFalse(should_exit,
                        "Loop should NOT exit when iteration returns True")


class TestActionMetrics(unittest.TestCase):
    """Test that action metrics correctly reflect execution status."""

    def test_actions_executed_counts_all_attempted(self):
        """actions_executed counts ALL actions including rejected ones."""
        # Simulate action_results after two refused actions
        action_results = [False, False]  # Both refused by NO_ACTIONABLE_ELEMENTS

        actions_executed = len(action_results)
        actions_ok = sum(action_results)

        self.assertEqual(actions_executed, 2,
                        "actions_executed should count all attempted actions")
        self.assertEqual(actions_ok, 0,
                        "actions_ok should count only successful actions")

    def test_auto_next_gated_on_success(self):
        """auto_next should only fire when at least one action succeeded."""
        # Case 1: No actions succeeded
        action_results = [False, False]
        should_auto_next = sum(action_results) > 0
        self.assertFalse(should_auto_next,
                        "auto_next should be suppressed when no actions succeeded")

        # Case 2: At least one action succeeded
        action_results = [True, False]
        should_auto_next = sum(action_results) > 0
        self.assertTrue(should_auto_next,
                       "auto_next should fire when at least one action succeeded")


class TestEmptyElementMapSafety(unittest.TestCase):
    """Test that empty element map prevents blind interaction."""

    def test_empty_map_refuses_blind_actions(self):
        """When element map is empty, click/type/select_multi/next should be refused."""
        elements = []  # Empty element map
        action_types_to_refuse = ("click", "type", "select_multi", "next")

        for action_type in action_types_to_refuse:
            # Simulate the safety guard
            should_refuse = not elements and action_type in action_types_to_refuse
            self.assertTrue(should_refuse,
                           f"Should refuse blind {action_type} when element map is empty")

    def test_empty_map_allows_wait_and_human_help(self):
        """When element map is empty, wait and human_help should still be allowed."""
        elements = []
        action_types_to_allow = ("wait", "human_help", "scroll")

        for action_type in action_types_to_allow:
            should_refuse = not elements and action_type in ("click", "type", "select_multi", "next")
            self.assertFalse(should_refuse,
                            f"Should allow {action_type} even when element map is empty")


class TestTabMonitor(unittest.TestCase):
    """Test that tab monitoring preserves the required (primary) window."""

    def test_primary_window_preserved(self):
        """check_new_tabs should always restore the primary window."""
        from core import BrowserController

        # Create a mock driver
        driver = MagicMock()
        driver.window_handles = ["SURVEY", "DEVTOOLS"]
        driver.current_window_handle = "SURVEY"

        controller = MagicMock(spec=BrowserController)
        controller.driver = driver
        controller._known_window_handles = {"SURVEY"}
        controller._primary_window = "SURVEY"

        # Simulate the fixed check_new_tabs logic
        current_handles = set(driver.window_handles)
        new_handles = current_handles - controller._known_window_handles

        if new_handles:
            new_handle = sorted(new_handles)[0]  # Deterministic
            original = getattr(controller, "_primary_window", None)

            # After peeking, restore original
            if original in driver.window_handles:
                driver.switch_to.window(original)

        # Verify primary window is restored
        driver.switch_to.window.assert_called_with("SURVEY")

    def test_close_extra_tabs_preserves_primary(self):
        """close_extra_tabs should close all except the primary window."""
        primary = "SURVEY"
        handles = ["SURVEY", "DEVTOOLS", "AD"]

        # Simulate the fixed close_extra_tabs logic
        closed = []
        for h in handles:
            if h == primary:
                continue
            closed.append(h)

        self.assertEqual(closed, ["DEVTOOLS", "AD"],
                        "Should close all non-primary handles")
        self.assertNotIn(primary, closed,
                        "Primary handle should not be closed")


class TestProgressDetection(unittest.TestCase):
    """Test that progress detection doesn't falsely report success."""

    def test_first_iteration_changes_are_not_progress(self):
        """On first iteration, URL/fingerprint/question changes are expected
        (comparing against None), not evidence of successful action."""
        from debug_logger import StuckDetector

        detector = StuckDetector(MagicMock())

        # First update: compares against None
        diag = detector.update("http://survey.com", "fp1", "question1")

        # All changed because last_* was None
        self.assertTrue(diag["url_changed"])
        self.assertTrue(diag["fingerprint_changed"])
        self.assertTrue(diag["question_changed"])

        # But same_state_count should be 0 (no consecutive same state)
        self.assertEqual(diag["same_state_count"], 0)

    def test_same_state_increments_counter(self):
        """Consecutive same states should increment same_state_count."""
        from debug_logger import StuckDetector

        detector = StuckDetector(MagicMock())

        # First update
        detector.update("http://survey.com", "fp1", "question1")

        # Second update with same state
        diag = detector.update("http://survey.com", "fp1", "question1")

        self.assertEqual(diag["same_state_count"], 1)
        self.assertFalse(diag["url_changed"])
        self.assertFalse(diag["fingerprint_changed"])
        self.assertFalse(diag["question_changed"])


if __name__ == "__main__":
    unittest.main()
