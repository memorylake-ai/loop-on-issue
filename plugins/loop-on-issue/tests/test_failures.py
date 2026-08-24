"""Telling "the server was busy" apart from "the agent did nothing".

They look identical from the outside — a non-zero exit and no output — and the
right response is opposite: one wants a quiet retry in a few minutes, the other
wants a human. Conflating them dresses up an outage as a problem with somebody's
requirement, which is the reading that erodes trust in the board.
"""

import unittest

import _bootstrap  # noqa: F401

from loopkit import failures


class Classify(unittest.TestCase):
    def test_overloaded(self):
        self.assertEqual(
            failures.classify("API Error: 529 Overloaded. This is a server-side issue", 1),
            failures.TRANSIENT)

    def test_rate_limited(self):
        self.assertEqual(failures.classify("API Error: 429 rate_limit_error", 1),
                         failures.TRANSIENT)

    def test_any_server_side_status(self):
        for status in ("500", "502", "503", "504", "529"):
            self.assertEqual(failures.classify("API Error: {} something".format(status), 1),
                             failures.TRANSIENT, status)

    def test_a_client_error_is_not_transient(self):
        # 400 and 401 do not fix themselves; retrying just burns the queue.
        for status in ("400", "401", "403", "404"):
            self.assertEqual(failures.classify("API Error: {} bad".format(status), 1),
                             failures.REAL, status)

    def test_network_faults(self):
        for text in ("ECONNRESET", "ETIMEDOUT", "socket hang up",
                     "connect ECONNREFUSED 1.2.3.4:443", "network error"):
            self.assertEqual(failures.classify(text, 1), failures.TRANSIENT, text)

    def test_an_overloaded_message_late_in_a_long_log_is_still_found(self):
        log = "\n".join(["thinking about it"] * 500 + ["API Error: 529 Overloaded"])
        self.assertEqual(failures.classify(log, 1), failures.TRANSIENT)

    def test_a_clean_exit_with_nothing_produced_is_real(self):
        # The agent ran, thought, and built nothing. Retrying changes nothing.
        self.assertEqual(failures.classify("I would have filed three issues.", 0),
                         failures.REAL)

    def test_a_session_collision_is_transient_because_the_retry_resumes(self):
        self.assertEqual(
            failures.classify("Error: Session ID abc is already in use.", 1),
            failures.TRANSIENT)

    def test_a_timeout_is_real(self):
        # It ran for the whole budget. Handing it the same budget again is not a
        # different experiment.
        self.assertEqual(failures.classify("timed out after 1800s and was stopped", 1),
                         failures.REAL)

    def test_empty_output(self):
        self.assertEqual(failures.classify("", 1), failures.REAL)

    def test_the_word_overloaded_in_prose_is_not_an_outage(self):
        # An agent writing about an overloaded queue in its own report must not
        # make the job look like a server fault.
        self.assertEqual(
            failures.classify("The worker pool was overloaded, so I split the slice.", 0),
            failures.REAL)


class Backoff(unittest.TestCase):
    def test_it_grows(self):
        delays = [failures.backoff(n) for n in range(1, 4)]
        self.assertEqual(delays, sorted(delays))
        self.assertLess(delays[0], delays[-1])

    def test_the_first_wait_is_short_enough_to_be_worth_waiting(self):
        self.assertLessEqual(failures.backoff(1), 120)

    def test_it_is_capped(self):
        self.assertLessEqual(failures.backoff(99), failures.MAX_BACKOFF)

    def test_attempt_zero_is_treated_as_the_first(self):
        self.assertEqual(failures.backoff(0), failures.backoff(1))


class ShouldRetry(unittest.TestCase):
    def test_a_transient_fault_within_budget_retries(self):
        self.assertTrue(failures.should_retry(failures.TRANSIENT, transient_failures=1, limit=3))

    def test_a_real_failure_never_retries(self):
        self.assertFalse(failures.should_retry(failures.REAL, transient_failures=0, limit=3))

    def test_the_budget_is_finite(self):
        # An outage that lasts all afternoon must not have this job hammering it.
        self.assertFalse(failures.should_retry(failures.TRANSIENT, transient_failures=3, limit=3))

    def test_a_zero_limit_disables_retries(self):
        self.assertFalse(failures.should_retry(failures.TRANSIENT, transient_failures=0, limit=0))


if __name__ == "__main__":
    unittest.main()
