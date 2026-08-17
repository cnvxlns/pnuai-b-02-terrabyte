"""Reconnect-capable USB serial JSON Lines reader."""

from __future__ import annotations

import logging
from threading import Event, Lock
from typing import Callable, Iterator, Protocol


LOGGER = logging.getLogger(__name__)


class SerialHandle(Protocol):
    def readline(self, size: int = ...) -> bytes: ...

    def write(self, data: bytes) -> int: ...

    def flush(self) -> None: ...

    def close(self) -> None: ...


SerialFactory = Callable[..., SerialHandle]


def _default_serial_factory(**kwargs: object) -> SerialHandle:
    try:
        import serial
    except ImportError as exc:
        raise RuntimeError("pyserial is required to read the Arduino") from exc
    return serial.Serial(**kwargs)


class SerialLineReader:
    """A serial link. Reads in the caller's thread, writes from any other.

    The name predates the link carrying traffic in both directions; it now also
    sends commands down to the Arduino. Reading and writing are genuinely
    concurrent — the read blocks for up to ``timeout_seconds`` and a pump
    command cannot wait behind it — so the handle is shared between threads and
    ``_write_lock`` exists only to serialise writers against teardown. It
    deliberately does not cover ``readline``: pyserial permits a write while a
    read is blocked, and putting the read under the same lock would stall every
    command for the length of a read timeout.
    """

    def __init__(
        self,
        *,
        port: str,
        baudrate: int,
        timeout_seconds: float,
        reconnect_seconds: float,
        max_line_bytes: int,
        factory: SerialFactory = _default_serial_factory,
    ) -> None:
        self.port = port
        self.baudrate = baudrate
        self.timeout_seconds = timeout_seconds
        self.reconnect_seconds = reconnect_seconds
        self.max_line_bytes = max_line_bytes
        self.factory = factory
        self._write_lock = Lock()
        # Published by lines() while a link is up, cleared before the handle is
        # closed. None means "no cable right now", which write_line reports as a
        # refusal rather than an exception: a command that cannot reach the pump
        # has to be answerable to the backend, not merely logged.
        self._handle: SerialHandle | None = None

    def write_line(self, payload: bytes) -> bool:
        """Send one newline-framed message. ``False`` if it could not be sent.

        Returning a bool rather than raising is the point: every caller has to
        tell the backend something, and "the Arduino is unreachable" is a
        reportable outcome, not an error to propagate. A caller that treats the
        return value as advisory will silently claim to have watered a plant.

        The trailing newline is added here because it is framing, not content —
        the firmware reads to ``\\n`` — and leaving it to callers means one of
        them eventually forgets and the firmware waits forever for a terminator.
        """

        if not payload:
            raise ValueError("refusing to write an empty serial message")
        body = payload[:-1] if payload.endswith(b"\n") else payload
        if b"\n" in body:
            # Two messages in one call would frame as two commands, and the
            # second would carry no ack correlation anyone is tracking.
            raise ValueError("serial message must not contain an embedded newline")

        with self._write_lock:
            handle = self._handle
            if handle is None:
                LOGGER.warning("serial write refused, link down port=%s", self.port)
                return False
            try:
                handle.write(body + b"\n")
                # Without the flush the bytes can sit in the OS buffer past the
                # command's TTL; the firmware would then act on a command the
                # backend has already expired.
                handle.flush()
            except Exception as exc:  # USB can vanish mid-write
                LOGGER.warning(
                    "serial write failed port=%s error=%s",
                    self.port,
                    type(exc).__name__,
                )
                return False
        return True

    def lines(self, stop: Event) -> Iterator[bytes]:
        while not stop.is_set():
            handle: SerialHandle | None = None
            try:
                handle = self.factory(
                    port=self.port,
                    baudrate=self.baudrate,
                    timeout=self.timeout_seconds,
                )
                with self._write_lock:
                    self._handle = handle
                LOGGER.info("serial connected port=%s", self.port)
                while not stop.is_set():
                    line = handle.readline(self.max_line_bytes + 1)
                    if not line:
                        continue
                    if len(line) > self.max_line_bytes:
                        LOGGER.warning("discarding oversized serial message")
                        if not line.endswith(b"\n"):
                            self._discard_remainder(handle, stop)
                        continue
                    if not line.endswith(b"\n"):
                        LOGGER.warning("discarding incomplete serial message")
                        self._discard_remainder(handle, stop)
                        continue
                    yield line
            except Exception as exc:  # the service must recover from USB driver errors
                LOGGER.warning("serial unavailable error=%s", type(exc).__name__)
            finally:
                # Unpublish before closing, and under the lock, so a writer
                # already inside write_line finishes against a live handle and
                # the next one sees the link as down instead of writing to a
                # closed file descriptor.
                with self._write_lock:
                    self._handle = None
                if handle is not None:
                    try:
                        handle.close()
                    except Exception:
                        LOGGER.debug("serial close failed", exc_info=True)
            if not stop.is_set():
                LOGGER.info("serial reconnect scheduled")
                stop.wait(self.reconnect_seconds)

    def _discard_remainder(self, handle: SerialHandle, stop: Event) -> None:
        while not stop.is_set():
            chunk = handle.readline(self.max_line_bytes + 1)
            if not chunk or chunk.endswith(b"\n"):
                return
