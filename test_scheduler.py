import unittest
from service import next_delay

class SchedulerTests(unittest.TestCase):
    def test_fallback_until_webhook(self):
        self.assertEqual(next_delay(False,[],{},'write'),300)
    def test_idle_after_webhook(self):
        self.assertIsNone(next_delay(True,[{'status':'unchanged','file':'/comics/a/x.cbz'}],{},'write'))
    def test_settling_followup(self):
        self.assertEqual(next_delay(True,[{'status':'settling','file':'/comics/a/x.cbz'}],{},'write'),300)
    def test_retries(self):
        for state in ({'pending_refresh':[1]}, {'pending_folders':['/comics/a']}):
            self.assertEqual(next_delay(True,[],state,'write'),300)
        self.assertEqual(next_delay(True,[],{},'write',failed=True),300)
    def test_review_does_not_spin(self):
        self.assertIsNone(next_delay(True,[{'status':'review','file':'/comics/a/x.cbr'}],
                                    {'pending_folders':['/comics/a']},'write'))
    def test_preview_does_not_retry_writes(self):
        self.assertIsNone(next_delay(True,[],{'pending_refresh':[1]},'preview'))
