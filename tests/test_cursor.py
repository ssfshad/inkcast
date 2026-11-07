import unittest
import sys

class TestCursorNormalization(unittest.TestCase):
    def test_get_normalized_cursor(self):
        import inkcast
        if sys.platform != "win32":
            return
        self.assertTrue(hasattr(inkcast, "get_normalized_cursor"))
        pos = inkcast.get_normalized_cursor(1920, 1080)
        self.assertIsInstance(pos, tuple)
        self.assertEqual(len(pos), 2)
        x, y = pos
        self.assertGreaterEqual(x, 0.0)
        self.assertLessEqual(x, 1.0)
        self.assertGreaterEqual(y, 0.0)
        self.assertLessEqual(y, 1.0)

if __name__ == "__main__":
    unittest.main()

