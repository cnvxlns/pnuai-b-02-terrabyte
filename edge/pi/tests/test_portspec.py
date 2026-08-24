import unittest

from terrabyte_edge.portspec import (
    PortInfo,
    PortResolutionError,
    UsbSpec,
    parse_spec,
    resolve,
    resolving_factory,
)


CH340 = PortInfo("COM7", 0x1A86, 0x7523, None, "USB-SERIAL CH340")
CH340_OTHER = PortInfo("COM9", 0x1A86, 0x7523, None, "USB-SERIAL CH340")
FTDI = PortInfo("COM3", 0x0403, 0x6001, "A50285BI", "FT232R USB UART")
BLUETOOTH = PortInfo("COM14", None, None, None, "Standard Serial over Bluetooth link")


class ParseSpecTests(unittest.TestCase):
    def test_literal_paths_are_not_usb_specs(self) -> None:
        for literal in ("COM7", "/dev/ttyUSB0", "/dev/serial/by-id/usb-1a86"):
            self.assertIsNone(parse_spec(literal))

    def test_vendor_and_product_only(self) -> None:
        self.assertEqual(parse_spec("usb:1a86:7523"), UsbSpec(0x1A86, 0x7523, None))

    def test_serial_number_is_optional_but_kept(self) -> None:
        self.assertEqual(
            parse_spec("usb:0403:6001:A50285BI"), UsbSpec(0x0403, 0x6001, "A50285BI")
        )

    def test_empty_serial_number_is_treated_as_absent(self) -> None:
        self.assertEqual(parse_spec("usb:1a86:7523:"), UsbSpec(0x1A86, 0x7523, None))

    def test_malformed_specs_are_rejected(self) -> None:
        for bad in ("usb:1a86", "usb:zzzz:7523", "usb:1a86:7523:x:y", "usb:10000:1"):
            with self.assertRaises(PortResolutionError):
                parse_spec(bad)


class ResolveTests(unittest.TestCase):
    def test_literal_path_passes_through_without_enumeration(self) -> None:
        self.assertEqual(resolve("COM7", ports=[]), "COM7")

    def test_single_match_resolves(self) -> None:
        self.assertEqual(resolve("usb:1a86:7523", ports=[BLUETOOTH, CH340]), "COM7")

    def test_serial_number_selects_between_identical_boards(self) -> None:
        labelled = PortInfo("COM9", 0x1A86, 0x7523, "SERIAL-B")
        ports = [PortInfo("COM7", 0x1A86, 0x7523, "SERIAL-A"), labelled]
        self.assertEqual(resolve("usb:1a86:7523:SERIAL-B", ports=ports), "COM9")

    def test_serial_number_match_is_case_insensitive(self) -> None:
        ports = [PortInfo("COM9", 0x1A86, 0x7523, "serial-b")]
        self.assertEqual(resolve("usb:1a86:7523:SERIAL-B", ports=ports), "COM9")

    def test_no_match_names_what_was_available(self) -> None:
        with self.assertRaises(PortResolutionError) as caught:
            resolve("usb:1a86:7523", ports=[FTDI, BLUETOOTH])
        self.assertIn("usb:0403:6001", str(caught.exception))

    def test_two_identical_boards_refuse_to_resolve(self) -> None:
        """CH340 bridges commonly report no serial number at all.

        Picking the first match would silently capture the wrong pot, and once
        this resolution feeds an actuator path it would command the wrong board.
        """

        with self.assertRaises(PortResolutionError) as caught:
            resolve("usb:1a86:7523", ports=[CH340, CH340_OTHER])
        message = str(caught.exception)
        self.assertIn("refusing to guess", message)
        self.assertIn("usb:VID:PID:SERIAL", message)

    def test_spec_with_serial_does_not_match_a_port_without_one(self) -> None:
        with self.assertRaises(PortResolutionError):
            resolve("usb:1a86:7523:SERIAL-A", ports=[CH340])


class ResolvingFactoryTests(unittest.TestCase):
    def test_port_is_re_resolved_on_every_connect(self) -> None:
        """A mid-night re-enumeration must be followed, not fatal."""

        devices = iter(["COM7", "COM12"])
        opened: list[str] = []

        def fake_inner(**kwargs):
            opened.append(kwargs["port"])
            return object()

        import terrabyte_edge.portspec as portspec

        factory = resolving_factory("usb:1a86:7523", inner=fake_inner)
        original = portspec.resolve

        try:
            portspec.resolve = lambda spec, **_kw: next(devices)  # type: ignore[assignment]
            factory(port="usb:1a86:7523", baudrate=115200, timeout=1.0)
            factory(port="COM7", baudrate=115200, timeout=1.0)
        finally:
            portspec.resolve = original  # type: ignore[assignment]

        self.assertEqual(opened, ["COM7", "COM12"])

    def test_other_serial_kwargs_are_preserved(self) -> None:
        seen: dict[str, object] = {}

        def fake_inner(**kwargs):
            seen.update(kwargs)
            return object()

        factory = resolving_factory("COM7", inner=fake_inner)
        factory(port="COM7", baudrate=115200, timeout=1.5)

        self.assertEqual(seen["baudrate"], 115200)
        self.assertEqual(seen["timeout"], 1.5)
        self.assertEqual(seen["port"], "COM7")


if __name__ == "__main__":
    unittest.main()
