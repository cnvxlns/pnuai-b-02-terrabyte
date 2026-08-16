from __future__ import annotations

from io import BytesIO
import json
import unittest
from urllib.error import HTTPError, URLError

from terrabyte_edge.backend import BackendClient, Delivery
from terrabyte_edge.protocol import Event


def event(ppfd: float | None = 300.0) -> Event:
    return Event(
        event_id="event-123",
        context_id="ctx/id",
        captured_at_utc="2026-07-21T04:05:06Z",
        node_id="node-1",
        sequence=1,
        uptime_ms=100,
        air_temperature_c=20.0,
        relative_humidity_pct=50.0,
        ppfd_umol_m2_s=ppfd,
    )


class FakeResponse:
    def __init__(self, status: int) -> None:
        self.status = status
        self.headers: dict[str, str] = {}

    def read(self, amount: int = -1) -> bytes:
        return b"{}"

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None


class BackendTests(unittest.TestCase):
    def client(self, transport):
        return BackendClient(
            url_for_context=lambda context_id: (
                "https://api.example.test/api/crop-contexts/"
                f"{context_id}/environment-observations"
            ),
            device_id="gateway-1",
            token="super-secret",
            timeout_seconds=4.0,
            transport=transport,
        )

    def test_created_request_matches_contract_and_authenticates_device(self) -> None:
        captured = {}

        def transport(request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return FakeResponse(201)

        result = self.client(transport).send(event())

        self.assertIs(result.outcome, Delivery.DELIVERED)
        request = captured["request"]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer super-secret")
        self.assertEqual(request.get_header("X-device-id"), "gateway-1")
        self.assertEqual(request.get_header("X-arduino-node-id"), "node-1")
        self.assertEqual(request.get_header("Idempotency-key"), "event-123")
        self.assertEqual(captured["timeout"], 4.0)
        self.assertEqual(
            request.full_url,
            "https://api.example.test/api/crop-contexts/ctx/id/environment-observations",
        )
        self.assertEqual(
            json.loads(request.data),
            {
                "capturedAtUtc": "2026-07-21T04:05:06Z",
                "airTemperatureC": 20.0,
                "relativeHumidityPct": 50.0,
                "ppfdUmolM2S": 300.0,
                "inputContract": "perfect_calibrated_v1",
            },
        )

    def test_duplicate_observation_is_delivered(self) -> None:
        error = HTTPError(
            "https://api.example.test",
            409,
            "Conflict",
            {},
            BytesIO(b'{"code":"DUPLICATE_OBSERVATION"}'),
        )

        def transport(_request, _timeout):
            raise error

        result = self.client(transport).send(event())
        self.assertIs(result.outcome, Delivery.DELIVERED)

    def test_null_ppfd_is_sent_explicitly(self) -> None:
        captured = {}

        def transport(request, _timeout):
            captured["body"] = json.loads(request.data)
            return FakeResponse(201)

        result = self.client(transport).send(event(ppfd=None))

        self.assertIs(result.outcome, Delivery.DELIVERED)
        self.assertIn("ppfdUmolM2S", captured["body"])
        self.assertIsNone(captured["body"]["ppfdUmolM2S"])

    def test_retryable_status_honors_retry_after(self) -> None:
        error = HTTPError(
            "https://api.example.test",
            429,
            "Too Many Requests",
            {"Retry-After": "17"},
            BytesIO(b"{}"),
        )

        def transport(_request, _timeout):
            raise error

        result = self.client(transport).send(event())
        self.assertIs(result.outcome, Delivery.RETRY)
        self.assertEqual(result.retry_after_seconds, 17.0)

    def test_network_and_server_errors_retry_but_bad_request_is_dead(self) -> None:
        cases = [
            (URLError("offline"), Delivery.RETRY),
            (
                HTTPError("https://x", 503, "Down", {}, BytesIO(b"{}")),
                Delivery.RETRY,
            ),
            (
                HTTPError(
                    "https://x",
                    400,
                    "Bad",
                    {},
                    BytesIO(b'{"code":"INVALID_CANONICAL_VALUE"}'),
                ),
                Delivery.DEAD,
            ),
            (
                HTTPError("https://x", 401, "Unauthorized", {}, BytesIO(b"{}")),
                Delivery.RETRY,
            ),
            (
                HTTPError("https://x", 404, "Not found", {}, BytesIO(b"{}")),
                Delivery.RETRY,
            ),
        ]
        for error, expected in cases:
            with self.subTest(error=error):
                def transport(_request, _timeout, raised=error):
                    raise raised

                self.assertIs(self.client(transport).send(event()).outcome, expected)


if __name__ == "__main__":
    unittest.main()
