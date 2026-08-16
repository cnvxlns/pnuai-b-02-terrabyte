"""nmcli wrapper, driven by a fake runner.

The connect path is never exercised against real hardware in CI or during
development on a remote board: joining a different network drops the SSH
session that is doing the testing, with no way back in.
"""

import unittest

from terrabyte_edge.netconfig import AccessPoint, CommandResult, WifiManager


class FakeRunner:
    def __init__(self, responses: dict[str, CommandResult]) -> None:
        self.responses = responses
        self.calls: list[tuple[tuple[str, ...], str | None]] = []

    def __call__(self, command, stdin, timeout) -> CommandResult:
        self.calls.append((tuple(command), stdin))
        for key, response in self.responses.items():
            if key in " ".join(command):
                return response
        return CommandResult(False, "", "unexpected command")


SCAN_OUTPUT = "\n".join(
    [
        "HomeNet-5G:74:WPA2",
        "HomeNet-5G:41:WPA2",  # same SSID, weaker radio
        "PNU-AI-Lab:55:WPA2",
        "OpenCafe:30:",
        ":22:WPA2",  # hidden network, no SSID
    ]
)


class ScanTests(unittest.TestCase):
    def test_parses_and_sorts_by_signal(self) -> None:
        manager = WifiManager(FakeRunner({"wifi list": CommandResult(True, SCAN_OUTPUT, "")}))
        points = manager.scan()
        self.assertEqual([p.ssid for p in points], ["HomeNet-5G", "PNU-AI-Lab", "OpenCafe"])

    def test_duplicate_ssid_keeps_the_strongest(self) -> None:
        manager = WifiManager(FakeRunner({"wifi list": CommandResult(True, SCAN_OUTPUT, "")}))
        self.assertEqual(manager.scan()[0].signal, 74)

    def test_hidden_networks_are_dropped(self) -> None:
        manager = WifiManager(FakeRunner({"wifi list": CommandResult(True, SCAN_OUTPUT, "")}))
        self.assertNotIn("", [p.ssid for p in manager.scan()])

    def test_open_network_is_marked_unsecured(self) -> None:
        manager = WifiManager(FakeRunner({"wifi list": CommandResult(True, SCAN_OUTPUT, "")}))
        cafe = next(p for p in manager.scan() if p.ssid == "OpenCafe")
        self.assertFalse(cafe.secured)

    def test_escaped_colon_in_ssid(self) -> None:
        """nmcli -t escapes colons, and SSIDs are allowed to contain them."""

        manager = WifiManager(
            FakeRunner({"wifi list": CommandResult(True, r"my\:net:60:WPA2", "")})
        )
        self.assertEqual(manager.scan()[0].ssid, "my:net")

    def test_failed_scan_is_empty_not_an_exception(self) -> None:
        manager = WifiManager(FakeRunner({"wifi list": CommandResult(False, "", "boom")}))
        self.assertEqual(manager.scan(), [])


class ConnectTests(unittest.TestCase):
    def test_password_goes_to_stdin_never_argv(self) -> None:
        """argv is world-readable through ps; the PSK must not appear there."""

        runner = FakeRunner({"wifi connect": CommandResult(True, "connected", "")})
        WifiManager(runner).connect("HomeNet-5G", "hunter2-secret")

        command, stdin = runner.calls[0]
        self.assertNotIn("hunter2-secret", " ".join(command))
        self.assertIn("--ask", command)
        self.assertEqual(stdin, "hunter2-secret\n")

    def test_open_network_needs_no_ask(self) -> None:
        runner = FakeRunner({"wifi connect": CommandResult(True, "connected", "")})
        WifiManager(runner).connect("OpenCafe", None)

        command, stdin = runner.calls[0]
        self.assertNotIn("--ask", command)
        self.assertIsNone(stdin)

    def test_failure_surfaces_nmcli_reason(self) -> None:
        """'Secrets were required but not provided' tells the operator the key
        was wrong; a generic '실패' does not."""

        runner = FakeRunner(
            {
                "wifi connect": CommandResult(
                    False, "", "Error: Secrets were required, but not provided."
                )
            }
        )
        result = WifiManager(runner).connect("HomeNet-5G", "wrong")
        self.assertFalse(result.ok)
        self.assertIn("Secrets were required", result.error)


class StatusTests(unittest.TestCase):
    def test_active_ssid(self) -> None:
        manager = WifiManager(
            FakeRunner(
                {
                    "connection show": CommandResult(
                        True, "SK_C348_5G:802-11-wireless\nlo:loopback", ""
                    )
                }
            )
        )
        self.assertEqual(manager.active_ssid(), "SK_C348_5G")

    def test_no_wireless_connection(self) -> None:
        manager = WifiManager(
            FakeRunner({"connection show": CommandResult(True, "lo:loopback", "")})
        )
        self.assertIsNone(manager.active_ssid())

    def test_connectivity(self) -> None:
        full = WifiManager(FakeRunner({"general status": CommandResult(True, "full", "")}))
        self.assertTrue(full.has_route())
        limited = WifiManager(
            FakeRunner({"general status": CommandResult(True, "limited", "")})
        )
        self.assertFalse(limited.has_route())


class SignalBarsTests(unittest.TestCase):
    def test_bars_are_always_four_cells(self) -> None:
        for signal in (0, 1, 30, 55, 74, 100):
            self.assertEqual(len(AccessPoint("x", signal, "WPA2").bars), 4)

    def test_stronger_signal_never_shows_fewer_bars(self) -> None:
        previous = 0
        for signal in range(0, 101, 5):
            filled = AccessPoint("x", signal, "WPA2").bars.count("█")
            self.assertGreaterEqual(filled, previous)
            previous = filled


if __name__ == "__main__":
    unittest.main()
