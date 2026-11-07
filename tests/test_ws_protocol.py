import unittest
from unittest.mock import MagicMock

class TestWsProtocol(unittest.TestCase):
    def test_hello_and_mouse_dispatch(self):
        import inkcast
        server = inkcast.Server(bridge=MagicMock(), aspect=16/9)
        server.auth_manager.set_pin("123456")
        
        # Test auth with token
        token = server.auth_manager.issue_token()
        self.assertTrue(server.auth_manager.validate_token(token))
        
        # Test mouse dispatch blocked when mouse_enabled is False
        server.mouse_enabled = False
        res = server.handle_mouse_msg({"t": "mouse", "action": "click", "btn": "left"}, is_authorized=True)
        self.assertFalse(res) # blocked by killswitch

        # Test mouse dispatch allowed when mouse_enabled is True and authorized
        server.mouse_enabled = True
        res = server.handle_mouse_msg({"t": "mouse", "action": "move_rel", "dx": 0, "dy": 0}, is_authorized=True)
        self.assertTrue(res)

        # Test mouse dispatch blocked when unauthorized
        res = server.handle_mouse_msg({"t": "mouse", "action": "move_rel", "dx": 0, "dy": 0}, is_authorized=False)
        self.assertFalse(res)

if __name__ == "__main__":
    unittest.main()
