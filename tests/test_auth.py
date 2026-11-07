import unittest
from unittest.mock import MagicMock

class TestAuthManager(unittest.TestCase):
    def test_auth_manager_pin_and_tokens(self):
        from inkcast import AuthManager
        auth = AuthManager(pin_hash="") # No PIN configured
        
        # When no PIN is set, fail-closed
        ok, msg = auth.validate_pin("123456", "1.2.3.4")
        self.assertFalse(ok)
        self.assertIn("locked", msg.lower())

        # Set a PIN
        auth.set_pin("654321")
        # Incorrect PIN
        ok, msg = auth.validate_pin("111111", "1.2.3.4")
        self.assertFalse(ok)
        self.assertIn("incorrect", msg.lower())

        # Correct PIN
        ok, token = auth.validate_pin("654321", "1.2.3.4")
        self.assertTrue(ok)
        self.assertTrue(len(token) > 10)
        self.assertTrue(auth.validate_token(token))

        # Changing PIN invalidates existing tokens
        auth.set_pin("999999")
        self.assertFalse(auth.validate_token(token))

    def test_rate_limiting(self):
        from inkcast import AuthManager
        auth = AuthManager(pin_hash="")
        auth.set_pin("654321")
        ip = "192.168.10.50"
        for _ in range(5):
            ok, _ = auth.validate_pin("000000", ip)
            self.assertFalse(ok)
        # 6th attempt should be rate limited
        ok, msg = auth.validate_pin("654321", ip)
        self.assertFalse(ok)
        self.assertIn("too many", msg.lower())

    def test_remote_classification(self):
        from inkcast import is_remote_request, get_client_ip
        # Mock request with CF-Connecting-IP
        req_cf = MagicMock()
        req_cf.headers = {"CF-Connecting-IP": "203.0.113.195"}
        req_cf.host = "xxxx.trycloudflare.com"
        self.assertTrue(is_remote_request(req_cf))
        self.assertEqual(get_client_ip(req_cf), "203.0.113.195")

        # Mock direct LAN request
        req_lan = MagicMock()
        req_lan.headers = {}
        req_lan.host = "192.168.1.5:8765"
        req_lan.remote = "192.168.1.100"
        self.assertFalse(is_remote_request(req_lan))
        self.assertEqual(get_client_ip(req_lan), "192.168.1.100")

if __name__ == "__main__":
    unittest.main()
