from threading import Event
import unittest

from terrabyte_edge.serial_reader import SerialLineReader


class FakeSerial:
    def __init__(self, lines, write_error=None):
        self.lines = list(lines)
        self.closed = False
        self.written = []
        self.flushes = 0
        self.write_error = write_error

    def readline(self, _size=0):
        if self.lines:
            return self.lines.pop(0)
        return b""

    def write(self, data):
        if self.write_error is not None:
            raise self.write_error
        if self.closed:
            raise AssertionError("wrote to a closed handle")
        self.written.append(data)
        return len(data)

    def flush(self):
        self.flushes += 1

    def close(self):
        self.closed = True


class SerialReaderTests(unittest.TestCase):
    def test_reads_complete_line_and_closes_port(self) -> None:
        handle = FakeSerial([b'{"ok":true}\n'])
        calls = []

        def factory(**kwargs):
            calls.append(kwargs)
            return handle

        reader = SerialLineReader(
            port="/dev/serial/by-id/test",
            baudrate=115200,
            timeout_seconds=1.0,
            reconnect_seconds=0.1,
            max_line_bytes=100,
            factory=factory,
        )
        generator = reader.lines(Event())
        self.assertEqual(next(generator), b'{"ok":true}\n')
        generator.close()

        self.assertTrue(handle.closed)
        self.assertEqual(calls[0]["port"], "/dev/serial/by-id/test")

    def test_oversized_line_is_discarded(self) -> None:
        handle = FakeSerial([b"x" * 101 + b"\n", b'{"ok":true}\n'])
        reader = SerialLineReader(
            port="test",
            baudrate=115200,
            timeout_seconds=1.0,
            reconnect_seconds=0.1,
            max_line_bytes=100,
            factory=lambda **_kwargs: handle,
        )
        generator = reader.lines(Event())
        self.assertEqual(next(generator), b'{"ok":true}\n')
        generator.close()


class WriteLineTests(unittest.TestCase):
    """The downlink half. Commands go out on the same handle the reader owns."""

    def reader(self, handle):
        return SerialLineReader(
            port="/dev/serial/by-id/test",
            baudrate=115200,
            timeout_seconds=1.0,
            reconnect_seconds=0.1,
            max_line_bytes=100,
            factory=lambda **_kwargs: handle,
        )

    def test_a_command_is_framed_and_flushed(self) -> None:
        handle = FakeSerial([b'{"ok":true}\n'])
        reader = self.reader(handle)
        generator = reader.lines(Event())
        next(generator)

        self.assertTrue(reader.write_line(b'{"t":"cmd","ms":30}'))
        self.assertEqual(handle.written, [b'{"t":"cmd","ms":30}\n'])
        # Unflushed bytes can outlive the command's TTL in the OS buffer.
        self.assertEqual(handle.flushes, 1)
        generator.close()

    def test_a_trailing_newline_is_not_doubled(self) -> None:
        handle = FakeSerial([b'{"ok":true}\n'])
        reader = self.reader(handle)
        generator = reader.lines(Event())
        next(generator)

        reader.write_line(b'{"t":"ka"}\n')
        self.assertEqual(handle.written, [b'{"t":"ka"}\n'])
        generator.close()

    def test_writing_with_no_link_is_refused_not_raised(self) -> None:
        """The caller owes the backend an answer, so this is a value not a crash."""

        handle = FakeSerial([])
        self.assertFalse(self.reader(handle).write_line(b'{"t":"cmd"}'))
        self.assertEqual(handle.written, [])

    def test_the_link_going_down_takes_the_write_path_with_it(self) -> None:
        handle = FakeSerial([b'{"ok":true}\n'])
        reader = self.reader(handle)
        generator = reader.lines(Event())
        next(generator)
        generator.close()

        self.assertTrue(handle.closed)
        # Unpublished before the close, so this cannot reach a closed fd.
        self.assertFalse(reader.write_line(b'{"t":"cmd"}'))

    def test_a_failed_write_is_reported_rather_than_propagated(self) -> None:
        handle = FakeSerial([b'{"ok":true}\n'], write_error=OSError("USB gone"))
        reader = self.reader(handle)
        generator = reader.lines(Event())
        next(generator)

        self.assertFalse(reader.write_line(b'{"t":"cmd"}'))
        generator.close()

    def test_an_embedded_newline_is_rejected(self) -> None:
        """Two commands in one call; the second would have no correlation id."""

        handle = FakeSerial([b'{"ok":true}\n'])
        reader = self.reader(handle)
        generator = reader.lines(Event())
        next(generator)

        with self.assertRaises(ValueError):
            reader.write_line(b'{"t":"cmd"}\n{"t":"cmd"}')
        with self.assertRaises(ValueError):
            reader.write_line(b"")
        self.assertEqual(handle.written, [])
        generator.close()


if __name__ == "__main__":
    unittest.main()
