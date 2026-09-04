import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from gans.ente.api import APIError, EnteAPI


class EnteAPIQueryTests(unittest.TestCase):
    def test_plus_is_percent_encoded(self):
        # Ente's Go server form-decodes queries: a literal '+' arrives as a space, which
        # broke login for plus-addressed emails.
        self.assertEqual(EnteAPI.encode_query_component("alice+ente@example.com"), "alice%2Bente@example.com")

    def test_separators_and_spaces_are_encoded(self):
        self.assertEqual(EnteAPI.encode_query_component("a&b=c d"), "a%26b%3Dc%20d")

    def test_plain_values_pass_through(self):
        self.assertEqual(EnteAPI.encode_query_component("alice@example.com"), "alice@example.com")
        self.assertEqual(EnteAPI.encode_query_component("12345"), "12345")


class _Handler(BaseHTTPRequestHandler):
    requests = []

    def log_message(self, *_):  # quiet
        pass

    def _respond(self, status, body, content_type="application/json"):
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        _Handler.requests.append(("GET", self.path, dict(self.headers), None))
        if self.path.startswith("/users/srp/attributes"):
            self._respond(200, json.dumps({"attributes": {"srpUserID": "u"}}))
        elif self.path.startswith("/missing"):
            self._respond(404, "")
        elif self.path.startswith("/expired"):
            self._respond(401, json.dumps({"message": "token expired"}))
        elif self.path.startswith("/garbage"):
            self._respond(200, "<html>", "text/html")
        elif self.path.startswith("/empty"):
            self._respond(200, "")
        else:
            self._respond(500, "boom")

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8")
        _Handler.requests.append(("POST", self.path, dict(self.headers), body))
        self._respond(200, json.dumps({"ok": True}))


class EnteAPITransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), _Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def setUp(self):
        _Handler.requests.clear()
        self.api = EnteAPI(self.base)

    def test_get_sends_headers_and_encodes_query(self):
        result = self.api.get("users/srp/attributes", [("email", "alice+ente@example.com")], authenticated=False)
        self.assertEqual(result["attributes"]["srpUserID"], "u")
        method, path, headers, _ = _Handler.requests[-1]
        self.assertEqual(path, "/users/srp/attributes?email=alice%2Bente@example.com")
        self.assertEqual(headers["X-Client-Package"], "io.ente.auth")
        self.assertEqual(headers["Accept"], "application/json")
        self.assertNotIn("X-Auth-Token", headers)

    def test_auth_token_only_on_authenticated_requests(self):
        self.api.set_auth_token("tok==")
        self.api.get("users/srp/attributes", authenticated=False)
        self.assertNotIn("X-Auth-Token", _Handler.requests[-1][2])
        self.api.get("users/srp/attributes", authenticated=True)
        self.assertEqual(_Handler.requests[-1][2]["X-Auth-Token"], "tok==")

    def test_post_sends_json_body(self):
        self.api.post("users/ott", {"email": "a@b", "purpose": "login"}, authenticated=False)
        method, path, headers, body = _Handler.requests[-1]
        self.assertEqual(method, "POST")
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(json.loads(body), {"email": "a@b", "purpose": "login"})

    def test_http_errors_carry_status_and_message(self):
        with self.assertRaises(APIError) as context:
            self.api.get("missing", authenticated=False)
        self.assertEqual(context.exception.kind, "http")
        self.assertEqual(context.exception.status, 404)
        self.assertIn("404", str(context.exception))
        with self.assertRaises(APIError) as context:
            self.api.get("expired", authenticated=True)
        self.assertEqual(context.exception.status, 401)
        self.assertIn("expired", str(context.exception))
        self.assertIn("token expired", context.exception.body)

    def test_decoding_error(self):
        with self.assertRaises(APIError) as context:
            self.api.get("garbage", authenticated=False)
        self.assertEqual(context.exception.kind, "decoding")

    def test_empty_body_is_empty_dict(self):
        self.assertEqual(self.api.get("empty", authenticated=False), {})

    def test_transport_error(self):
        api = EnteAPI("http://127.0.0.1:1")  # nothing listens here
        with self.assertRaises(APIError) as context:
            api.get("x", authenticated=False)
        self.assertEqual(context.exception.kind, "transport")
        self.assertTrue(str(context.exception).startswith("Network error"))


if __name__ == "__main__":
    unittest.main()
