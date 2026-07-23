from __future__ import annotations

import unittest

from starlette.testclient import TestClient

from open_somnia.remote.relay import create_relay_app
from tests.remote_auth_support import BROWSER_ORIGIN, authenticate_connector, login, pair_device


class RemoteRelayTests(unittest.TestCase):
    def test_health_endpoint_reports_ready_without_storing_state(self) -> None:
        with TestClient(create_relay_app()) as client:
            response = client.get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ready"})

    def test_relay_forwards_requests_responses_and_events_without_interpreting_payloads(self) -> None:
        with TestClient(create_relay_app(administrators={"admin": "admin-password"})) as client:
            login(client)
            private_key, device_id = pair_device(client)
            with (
                client.websocket_connect(f"/ws/connector/{device_id}") as connector,
                client.websocket_connect(
                    f"/ws/client/{device_id}", headers={"origin": BROWSER_ORIGIN}
                ) as browser,
            ):
                authenticate_connector(connector, device_id, private_key)
                request = {
                    "kind": "request",
                    "request_id": "request-1",
                    "project_id": "project-1",
                    "method": "turn.start",
                    "params": {"session_id": "session-1", "user_input": "hello"},
                }
                browser.send_json(request)
                self.assertEqual(connector.receive_json(), request)

                response = {
                    "kind": "response",
                    "request_id": "request-1",
                    "ok": True,
                    "result": {"turn_id": "turn-1", "session_id": "session-1"},
                }
                connector.send_json(response)
                self.assertEqual(browser.receive_json(), response)

                event = {
                    "kind": "event",
                    "project_id": "project-1",
                    "event": {
                        "type": "assistant_delta",
                        "session_id": "session-1",
                        "turn_id": "turn-1",
                        "payload": {"delta": "streamed"},
                    },
                }
                connector.send_json(event)
                self.assertEqual(browser.receive_json(), event)

    def test_relay_rejects_content_when_device_is_offline_instead_of_queueing_it(self) -> None:
        with TestClient(create_relay_app(administrators={"admin": "admin-password"})) as client:
            login(client)
            _, device_id = pair_device(client)
            with client.websocket_connect(
                f"/ws/client/{device_id}", headers={"origin": BROWSER_ORIGIN}
            ) as browser:
                browser.send_json(
                    {
                        "kind": "request",
                        "request_id": "request-2",
                        "project_id": "project-1",
                        "method": "turn.start",
                        "params": {"session_id": "session-1", "user_input": "do not retain"},
                    }
                )

                self.assertEqual(
                    browser.receive_json(),
                    {
                        "kind": "response",
                        "request_id": "request-2",
                        "ok": False,
                        "error": "Device is offline.",
                    },
                )


if __name__ == "__main__":
    unittest.main()
