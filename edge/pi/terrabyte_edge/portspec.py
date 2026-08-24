"""Resolve a stable USB identifier to whatever device name the OS gave it.

On Linux the deployment uses ``/dev/serial/by-id/...``, which the kernel keeps
stable. Windows has no equivalent: the same board can come back as COM3, COM7
or COM12 depending on which physical port it was plugged into and what else has
been enumerated since. An unattended overnight capture that hard-codes a COM
number stops the first time the port re-enumerates.

So a port may be given either literally::

    COM7                        /dev/ttyUSB0        /dev/serial/by-id/usb-...

or as a USB identity that survives re-enumeration::

    usb:1a86:7523               vendor and product
    usb:1a86:7523:A50285BI      ...plus the serial number

Serial numbers are optional because the CH340 bridges these nodes use commonly
report none at all. When several boards share a vendor/product pair and no
serial number can tell them apart, resolution FAILS rather than picking the
first match: capturing a night of data from the wrong pot is worse than not
starting, and once an actuator path exists on this code the same ambiguity
would mean commanding the wrong board.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Iterable, Sequence


LOGGER = logging.getLogger(__name__)

USB_SPEC_PREFIX = "usb:"


class PortResolutionError(RuntimeError):
    """Raised when a USB spec matches no port, or more than one."""


@dataclass(frozen=True)
class UsbSpec:
    vendor_id: int
    product_id: int
    serial_number: str | None = None

    def matches(self, port: "PortInfo") -> bool:
        if port.vendor_id != self.vendor_id or port.product_id != self.product_id:
            return False
        if self.serial_number is None:
            return True
        if port.serial_number is None:
            return False
        return port.serial_number.upper() == self.serial_number.upper()

    def __str__(self) -> str:
        base = f"{USB_SPEC_PREFIX}{self.vendor_id:04x}:{self.product_id:04x}"
        return base if self.serial_number is None else f"{base}:{self.serial_number}"


@dataclass(frozen=True)
class PortInfo:
    """The subset of pyserial's ListPortInfo this module needs."""

    device: str
    vendor_id: int | None = None
    product_id: int | None = None
    serial_number: str | None = None
    description: str | None = None

    def as_spec(self) -> str | None:
        """The most specific usb: spec that would select this port."""

        if self.vendor_id is None or self.product_id is None:
            return None
        spec = f"{USB_SPEC_PREFIX}{self.vendor_id:04x}:{self.product_id:04x}"
        if self.serial_number:
            spec = f"{spec}:{self.serial_number}"
        return spec

    def describe(self) -> str:
        spec = self.as_spec() or "(no USB identity)"
        description = self.description or "unknown device"
        return f"{self.device:<12} {spec:<28} {description}"


def parse_spec(spec: str) -> UsbSpec | None:
    """Parse a ``usb:`` spec, or return None for a literal device path."""

    if not spec.lower().startswith(USB_SPEC_PREFIX):
        return None
    body = spec[len(USB_SPEC_PREFIX) :]
    parts = body.split(":")
    if len(parts) not in (2, 3):
        raise PortResolutionError(
            f"malformed USB spec {spec!r}; expected usb:VID:PID or usb:VID:PID:SERIAL"
        )
    try:
        vendor_id = int(parts[0], 16)
        product_id = int(parts[1], 16)
    except ValueError as exc:
        raise PortResolutionError(
            f"malformed USB spec {spec!r}; VID and PID must be hexadecimal"
        ) from exc
    if not 0 <= vendor_id <= 0xFFFF or not 0 <= product_id <= 0xFFFF:
        raise PortResolutionError(f"malformed USB spec {spec!r}; VID/PID out of range")
    serial_number = parts[2] if len(parts) == 3 and parts[2] else None
    return UsbSpec(vendor_id, product_id, serial_number)


def list_ports() -> list[PortInfo]:
    """Enumerate serial ports, or return an empty list if pyserial is absent."""

    try:
        from serial.tools import list_ports as pyserial_ports
    except ImportError:
        LOGGER.warning("pyserial is not installed; cannot enumerate serial ports")
        return []
    return [
        PortInfo(
            device=entry.device,
            vendor_id=entry.vid,
            product_id=entry.pid,
            serial_number=entry.serial_number,
            description=entry.description,
        )
        for entry in pyserial_ports.comports()
    ]


def resolve(spec: str, *, ports: Sequence[PortInfo] | None = None) -> str:
    """Resolve ``spec`` to a device name.

    A literal path is returned unchanged and is NOT checked for existence:
    the caller reconnects on failure anyway, and a board that is briefly
    unplugged must not be a fatal configuration error.
    """

    usb_spec = parse_spec(spec)
    if usb_spec is None:
        return spec

    available = list(ports) if ports is not None else list_ports()
    matches = [port for port in available if usb_spec.matches(port)]
    if not matches:
        raise PortResolutionError(
            f"no serial port matches {usb_spec}. Available: {_inventory(available)}"
        )
    if len(matches) > 1:
        detail = ", ".join(port.device for port in matches)
        hint = (
            "add the serial number as usb:VID:PID:SERIAL"
            if usb_spec.serial_number is None
            else "the serial numbers are not unique"
        )
        raise PortResolutionError(
            f"{len(matches)} ports match {usb_spec} ({detail}); refusing to guess - {hint}"
        )
    return matches[0].device


def _inventory(ports: Iterable[PortInfo]) -> str:
    entries = [port.as_spec() or port.device for port in ports]
    return ", ".join(entries) if entries else "none"


def resolving_factory(spec: str, *, inner=None):
    """Wrap a pyserial factory so the port is re-resolved on every connect.

    ``SerialLineReader`` takes its port once at construction and passes it to
    the factory on each reconnect. Re-resolving inside the factory is what
    makes a mid-night re-enumeration survivable, and it needs no change to
    ``SerialLineReader`` itself - which matters because that file is shared
    with the production gateway.
    """

    def factory(**kwargs: object):
        kwargs = dict(kwargs)
        device = resolve(spec)
        if device != kwargs.get("port"):
            LOGGER.info("resolved %s to %s", spec, device)
        kwargs["port"] = device
        if inner is not None:
            return inner(**kwargs)
        try:
            import serial
        except ImportError as exc:  # pragma: no cover - operator-facing
            raise RuntimeError("pyserial is required: pip install pyserial") from exc
        return serial.Serial(**kwargs)

    return factory
