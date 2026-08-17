"""Display logic, tested with no display.

Everything asserted here is a pure function of the snapshot, which is why the
Tk layer can stay a thin widget map with no decisions of its own.
"""

import unittest

from terrabyte_edge.ui.render import (
    QUEUE_ERROR,
    QUEUE_WARN,
    STALE_AFTER_SECONDS,
    build_view,
    format_age,
    format_uptime,
)

NOW = 1_700_000_000.0


def port(**overrides) -> dict:
    base = {
        "path": "/dev/ttyUSB0",
        "node_id": "terrabyte-node-01",
        "link": "up",
        "last_frame_epoch": NOW - 3,
        "frames": 10,
        "errors": 0,
        "fault": None,
        "fault_detail": None,
        "measurements": {
            "air_temperature_c": 27.14,
            "air_humidity_pct": 58.0,
            "plant_light_ppfd_umol_m2_s": 230.5,
            "soil_temperature_c": 21.4,
            "soil_moisture_pct": 31.2,
        },
    }
    base.update(overrides)
    return base


def snapshot(**overrides) -> dict:
    base = {
        "schema": 1,
        "generated_at_epoch": NOW,
        "started_at_epoch": NOW - 3600,
        "gateway_id": "orangepi-pro-01",
        "claim_code": "483920",
        "transport": {
            "kind": "mqtt",
            "connected": True,
            "last_error": None,
            "last_delivery_epoch": NOW - 2,
        },
        "outbox": {"pending": 0, "dead": 0},
        "ports": [port()],
        "events": [{"at_epoch": NOW - 5, "level": "info", "text": "전송 완료"}],
    }
    base.update(overrides)
    return base


class NominalTests(unittest.TestCase):
    def test_header(self) -> None:
        view = build_view(snapshot(), now_epoch=NOW)
        self.assertEqual(view.gateway_id, "orangepi-pro-01")
        self.assertEqual(view.claim_code, "483920")
        self.assertEqual(view.server_text, "연결됨")
        self.assertEqual(view.server_level, "ok")
        self.assertIsNone(view.banner)

    def test_always_renders_four_slots(self) -> None:
        """One Arduino plugged in still shows four pots, so an operator can see
        which ones are missing rather than guessing how many there should be."""

        view = build_view(snapshot(), now_epoch=NOW)
        self.assertEqual(len(view.rows), 4)
        self.assertEqual(view.rows[0].link_text, "정상")
        self.assertEqual(view.rows[1].link_text, "포트 없음")

    def test_metrics_are_formatted(self) -> None:
        row = build_view(snapshot(), now_epoch=NOW).rows[0]
        self.assertEqual(row.values, ("27.1℃", "58%", "230", "21.4℃", "31%"))
        self.assertEqual(row.last_seen, "3초 전")

    def test_absent_probe_stays_a_dash(self) -> None:
        """Zero would read as a real measurement, and for soil moisture as
        'bone dry' — the one reading that must never be fabricated."""

        view = build_view(
            snapshot(ports=[port(measurements={"air_temperature_c": 20.0})]),
            now_epoch=NOW,
        )
        self.assertEqual(view.rows[0].values[0], "20.0℃")
        self.assertEqual(view.rows[0].values[4], "—")


class DegradedTests(unittest.TestCase):
    def test_missing_snapshot_is_a_diagnostic_not_a_crash(self) -> None:
        view = build_view(None, now_epoch=NOW)
        self.assertIsNotNone(view.banner)
        self.assertEqual(view.banner.level, "error")
        self.assertIn("systemctl", view.footer)
        self.assertEqual(len(view.rows), 4)

    def test_stale_snapshot_is_called_out(self) -> None:
        view = build_view(
            snapshot(generated_at_epoch=NOW - STALE_AFTER_SECONDS - 40),
            now_epoch=NOW,
        )
        self.assertIsNotNone(view.banner)
        self.assertIn("응답 없음", view.banner.text)

    def test_fresh_snapshot_has_no_banner(self) -> None:
        view = build_view(
            snapshot(generated_at_epoch=NOW - STALE_AFTER_SECONDS + 1), now_epoch=NOW
        )
        self.assertIsNone(view.banner)

    def test_transport_disconnected_shows_the_reason(self) -> None:
        view = build_view(
            snapshot(
                transport={
                    "kind": "mqtt",
                    "connected": False,
                    "last_error": "not_connected",
                    "last_delivery_epoch": NOW - 600,
                }
            ),
            now_epoch=NOW,
        )
        self.assertEqual(view.server_level, "error")
        self.assertIn("not_connected", view.server_text)

    def test_never_delivered_reads_as_connecting_not_broken(self) -> None:
        view = build_view(
            snapshot(
                transport={"kind": "mqtt", "connected": False, "last_delivery_epoch": None}
            ),
            now_epoch=NOW,
        )
        self.assertEqual(view.server_level, "warn")
        self.assertEqual(view.server_text, "연결 시도 중")

    def test_queue_thresholds(self) -> None:
        def level(pending: int, dead: int = 0) -> str:
            return build_view(
                snapshot(outbox={"pending": pending, "dead": dead}), now_epoch=NOW
            ).queue_level

        self.assertEqual(level(0), "ok")
        self.assertEqual(level(QUEUE_WARN), "warn")
        self.assertEqual(level(QUEUE_ERROR), "error")
        # Anything quarantined is an error regardless of depth.
        self.assertEqual(level(0, dead=1), "error")

    def test_duplicate_node_outranks_link_state(self) -> None:
        """The port is electrically fine, which is exactly why it needs to be
        louder than a disconnected one."""

        view = build_view(
            snapshot(
                ports=[
                    port(
                        fault="duplicate_node",
                        fault_detail="node-01 이(가) 다른 포트에서도 보입니다",
                    )
                ]
            ),
            now_epoch=NOW,
        )
        self.assertEqual(view.rows[0].link_text, "중복 노드")
        self.assertEqual(view.rows[0].link_level, "error")
        self.assertIn("다른 포트", view.rows[0].fault)

    def test_unknown_node_row(self) -> None:
        view = build_view(
            snapshot(ports=[port(fault="unknown_node", fault_detail="허용 목록에 없습니다")]),
            now_epoch=NOW,
        )
        self.assertEqual(view.rows[0].link_text, "미등록 노드")
        self.assertEqual(view.rows[0].link_level, "error")

    def test_missing_claim_code_shows_placeholder_not_blank(self) -> None:
        view = build_view(snapshot(claim_code=""), now_epoch=NOW)
        self.assertEqual(view.claim_code, "——————")


class FormattingTests(unittest.TestCase):
    def test_age(self) -> None:
        self.assertEqual(format_age(None), "—")
        self.assertEqual(format_age(3), "3초 전")
        self.assertEqual(format_age(90), "1분 전")
        self.assertEqual(format_age(3700), "1시간 1분 전")
        self.assertEqual(format_age(90000), "1일 전")

    def test_negative_age_from_a_clock_step_reads_as_zero(self) -> None:
        self.assertEqual(format_age(-5), "0초 전")

    def test_uptime(self) -> None:
        self.assertEqual(format_uptime(45), "45초")
        self.assertEqual(format_uptime(600), "10분")
        self.assertEqual(format_uptime(3660), "1시간 1분")
        self.assertEqual(format_uptime(200000), "2일 7시간")


if __name__ == "__main__":
    unittest.main()
