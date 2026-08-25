import json
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from threading import Thread
from unittest.mock import MagicMock

from terrabyte_edge.ui.dashboard import Dashboard
from terrabyte_edge.ui.render import build_view
from terrabyte_edge.ui.text import render
from terrabyte_edge.ui.web import build_server, render_html


NOW = 1_800_000_000.0

HEALTHY = {
    "schema": 1,
    "generated_at_epoch": NOW,
    "started_at_epoch": NOW - 3600,
    "gateway_id": "orangepi-pro-01",
    "claim_code": "",
    "transport": {"connected": True, "last_error": None, "last_delivery_epoch": NOW - 1},
    "outbox": {"pending": 0, "dead": 0},
    "ports": [
        {
            "path": "/dev/cu.usbserial-10",
            "node_id": "terrabyte-node-001",
            "link": "up",
            "last_frame_epoch": NOW - 2,
            "measurements": {
                "air_temperature_c": 25.3,
                "air_humidity_pct": 62.5,
                "plant_light_ppfd_umol_m2_s": 5.4,
                "soil_temperature_c": 23.0,
                "soil_moisture_pct": 36.0,
            },
        }
    ],
    "events": [{"at_epoch": NOW - 30, "text": "브릿지 시작"}],
}

CLAIMABLE = {**HEALTHY, "claim_code": "483920"}

BROKEN = {
    **HEALTHY,
    # A past delivery is what distinguishes "끊김" from "연결 시도 중": a gateway
    # that has never delivered is still starting up, not broken.
    "transport": {"connected": False, "last_error": "broker unreachable", "last_delivery_epoch": NOW - 900},
    "outbox": {"pending": 720, "dead": 3},
    "ports": [{"path": "/dev/cu.usbserial-10", "node_id": None, "link": "down", "last_frame_epoch": None}],
}


def view(snapshot):
    return build_view(snapshot, now_epoch=NOW)


class TextRendererTests(unittest.TestCase):
    def test_shows_the_gateway_and_the_node_reading(self) -> None:
        out = render(view(HEALTHY))
        self.assertIn("orangepi-pro-01", out)
        self.assertIn("terrabyte-node-001", out)
        self.assertIn("25.3℃", out)
        self.assertIn("36%", out)

    def test_shows_the_grouped_claim_code_and_instruction(self) -> None:
        out = render(view(CLAIMABLE))
        self.assertIn("기기 등록 코드", out)
        self.assertIn("483 920", out)
        self.assertIn("앱에서 이 코드를 입력", out)

    def test_missing_claim_code_shows_a_grouped_placeholder(self) -> None:
        self.assertIn("——— ———", render(view(HEALTHY)))

    def test_columns_line_up_across_rows(self) -> None:
        """Korean is double-width in a terminal. Counting characters instead of
        columns produces a table that shifts by one cell per row, which is
        harder to read than no table at all."""

        lines = [line for line in render(view(HEALTHY)).splitlines() if "화분" in line]
        self.assertTrue(lines)
        starts = {line.index("화분") for line in lines}
        self.assertEqual(len(starts), 1)

    def test_a_broken_gateway_says_so_without_a_banner_colour(self) -> None:
        out = render(view(BROKEN))
        self.assertIn("broker unreachable", out)
        self.assertIn("720", out)

    def test_an_absent_snapshot_still_renders(self) -> None:
        """The board has to be readable before the bridge has ever written a
        snapshot; that is exactly when someone is looking at it."""

        out = render(view(None))
        self.assertTrue(out.strip())


class HtmlRendererTests(unittest.TestCase):
    def test_contains_the_reading_and_a_self_refresh(self) -> None:
        page = render_html(view(HEALTHY))
        self.assertIn("orangepi-pro-01", page)
        self.assertIn("25.3℃", page)
        self.assertIn("http-equiv=\"refresh\"", page)

    def test_shows_the_grouped_claim_code_and_instruction(self) -> None:
        page = render_html(view(CLAIMABLE))
        self.assertIn("기기 등록 코드", page)
        self.assertIn("483 920", page)
        self.assertIn("앱에서 이 코드를 입력", page)

    def test_missing_claim_code_shows_a_grouped_placeholder(self) -> None:
        self.assertIn("——— ———", render_html(view(HEALTHY)))

    def test_is_utf8_declared_and_mobile_sized(self) -> None:
        page = render_html(view(HEALTHY))
        self.assertIn("charset=\"utf-8\"", page)
        self.assertIn("width=device-width", page)

    def test_escapes_snapshot_text(self) -> None:
        """The snapshot carries a gateway id and error strings from outside this
        process. Neither is trusted markup."""

        hostile = {**BROKEN, "gateway_id": "<script>alert(1)</script>"}
        page = render_html(view(hostile))
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertIn("&lt;script&gt;", page)

    def test_link_level_reaches_the_markup(self) -> None:
        self.assertIn("class=\"ok\"", render_html(view(HEALTHY)))
        self.assertIn("class=\"error\"", render_html(view(BROKEN)))


class TkRendererTests(unittest.TestCase):
    def make_dashboard(self) -> Dashboard:
        dashboard = Dashboard.__new__(Dashboard)
        dashboard.banner = MagicMock()
        dashboard.gateway = MagicMock()
        dashboard.status = MagicMock()
        dashboard.claim_code = MagicMock()
        dashboard.rows = []
        dashboard.footer = MagicMock()
        return dashboard

    def test_apply_emits_the_grouped_claim_code_without_opening_a_window(self) -> None:
        dashboard = self.make_dashboard()
        dashboard.apply(view(CLAIMABLE))

        dashboard.claim_code.configure.assert_called_once_with(text="483 920")

    def test_apply_emits_a_grouped_placeholder_without_opening_a_window(self) -> None:
        dashboard = self.make_dashboard()
        dashboard.apply(view(HEALTHY))

        dashboard.claim_code.configure.assert_called_once_with(text="——— ———")


class ServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.snapshot = Path(self._tmp.name) / "status.json"
        self.snapshot.write_text(json.dumps(HEALTHY), encoding="utf-8")
        # Port 0 lets the OS pick, so a busy port never makes this flaky.
        self.server = build_server(
            snapshot_path=self.snapshot, host="127.0.0.1", port=0, clock=lambda: NOW
        )
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self._tmp.cleanup()

    def get(self, path: str):
        with urllib.request.urlopen(self.base + path, timeout=5) as response:
            return response.status, response.headers, response.read().decode("utf-8")

    def test_serves_the_board(self) -> None:
        status, headers, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn("terrabyte-node-001", body)

    def test_is_not_cached(self) -> None:
        """A cached copy would freeze the board at whatever the gateway looked
        like the first time someone opened it."""

        _, headers, _ = self.get("/")
        self.assertEqual(headers["Cache-Control"], "no-store")

    def test_json_view_matches_the_page(self) -> None:
        _, _, body = self.get("/status.json")
        payload = json.loads(body)
        self.assertEqual(payload["gateway_id"], "orangepi-pro-01")
        self.assertEqual(payload["rows"][0]["node_id"], "terrabyte-node-001")
        self.assertEqual(payload["rows"][0]["values"]["air_temperature_c"], "25.3℃")

    def test_healthz(self) -> None:
        status, _, body = self.get("/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(body, "ok")

    def test_unknown_path_is_404(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.get("/../etc/passwd")
        self.assertEqual(caught.exception.code, 404)

    def test_reflects_a_snapshot_written_after_start(self) -> None:
        """The page is rebuilt per request, so the bridge and the board do not
        have to be restarted together."""

        self.snapshot.write_text(json.dumps(BROKEN), encoding="utf-8")
        _, _, body = self.get("/")
        self.assertIn("broker unreachable", body)


if __name__ == "__main__":
    unittest.main()
