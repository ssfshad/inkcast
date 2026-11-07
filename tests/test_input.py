import unittest
import ctypes
from ctypes import wintypes

class TestInputStructures(unittest.TestCase):
    def test_structures_and_executor(self):
        import inkcast
        # Verify MOUSEINPUT size and field alignment
        self.assertTrue(hasattr(inkcast, "MOUSEINPUT"))
        self.assertTrue(hasattr(inkcast, "INPUT"))
        self.assertEqual(ctypes.sizeof(inkcast.INPUT), 40) # 64-bit Windows alignment
        
        # Verify InputExecutor exists and has single worker
        self.assertTrue(hasattr(inkcast, "InputExecutor"))
        executor = inkcast.InputExecutor()
        self.assertEqual(executor.executor._max_workers, 1)

        # Verify mouse injection function
        self.assertTrue(hasattr(inkcast, "inject_mouse_event"))
        # In CI/non-interactive environments SendInput returns 0 (Access Denied).
        # We test with mock to verify parameter dispatching, and also call directly if live.
        from unittest.mock import patch
        with patch.object(ctypes.windll.user32, "SendInput", return_value=1) as mock_send:
            # Test relative move
            result = inkcast.inject_mouse_event("move_rel", dx=5, dy=-10)
            self.assertTrue(result)
            self.assertTrue(mock_send.called)

            # Test submit_mouse via executor
            future = executor.submit_mouse("move_rel", dx=0, dy=0)
            self.assertTrue(future.result(timeout=1.0))

            # Test other actions
            self.assertTrue(inkcast.inject_mouse_event("click", btn="left"))
            self.assertTrue(inkcast.inject_mouse_event("click", btn="right"))
            self.assertTrue(inkcast.inject_mouse_event("down", btn="left"))
            self.assertTrue(inkcast.inject_mouse_event("up", btn="left"))
            self.assertTrue(inkcast.inject_mouse_event("down", btn="right"))
            self.assertTrue(inkcast.inject_mouse_event("up", btn="right"))
            self.assertTrue(inkcast.inject_mouse_event("scroll", dy=120))
            self.assertFalse(inkcast.inject_mouse_event("unknown_action"))

        executor.shutdown()

if __name__ == "__main__":
    unittest.main()
