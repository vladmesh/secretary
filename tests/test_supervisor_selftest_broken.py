import unittest


class BrokenSelftest(unittest.TestCase):
    def test_intentionally_failing(self):
        # Supervisor Test B: this MUST fail so smoke blocks promotion. Reverted after the test.
        self.assertTrue(False, "intentional supervisor selftest failure")


if __name__ == "__main__":
    unittest.main()
