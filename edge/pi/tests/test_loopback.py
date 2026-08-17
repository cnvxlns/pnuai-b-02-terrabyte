"""The software Arduino, and one pass of the serial loop through the bridge."""

import json
from threading import Event
import unittest

from terrabyte_edge.loopback import ABS_MAX_RUN_MS, COMMAND_ID_RING, LoopbackArduino
from terrabyte_edge.serial_reader import SerialLineReader


def reader_for(handle: LoopbackArduino) -> SerialLineReader:
    return SerialLineReader(
        port="loopback-0",
        baudrate=115200,
        timeout_seconds=0.01,
        reconnect_seconds=0.01,
        max_line_bytes=512,
        factory=handle.as_factory(),
    )


def acks(handle: LoopbackArduino) -> list[dict]:
    """Drain everything readable, keeping only the ack frames."""

    frames = []
    while True:
        line = handle.readline()
        if not line:
            return frames
        message = json.loads(line)
        if message.get("t") == "ack":
            frames.append(message)


class CommandHandlingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.handle = LoopbackArduino(node_id="terrabyte-node-01")

    def send(self, message: dict) -> list[dict]:
        self.handle.write(json.dumps(message).encode() + b"\n")
        return acks(self.handle)

    def test_a_pump_command_is_accepted_then_completed(self) -> None:
        """Two lines, not one. A consumer must be able to see the middle state."""

        answered = self.send(
            {"t": "cmd", "id": "01J8F3", "act": "pump", "ms": 18000, "ml": 120}
        )
        self.assertEqual([ack["ph"] for ack in answered], ["accepted", "completed"])
        self.assertEqual(answered[1]["ms"], 18000)
        self.assertEqual(answered[1]["stop"], "volume_reached")
        self.assertTrue(all(ack["id"] == "01J8F3" for ack in answered))

    def test_an_over_long_command_is_clamped_and_says_so(self) -> None:
        """G1. The reported ms is what ran, not what was asked for."""

        answered = self.send({"t": "cmd", "id": "long", "act": "pump", "ms": 60000})
        self.assertEqual(answered[1]["ms"], ABS_MAX_RUN_MS)
        self.assertEqual(answered[1]["stop"], "max_runtime")

    def test_a_repeated_command_id_is_rejected(self) -> None:
        """QoS 1 allows a duplicate at both hops; the ring is the last defence."""

        self.send({"t": "cmd", "id": "same", "act": "pump", "ms": 1000})
        answered = self.send({"t": "cmd", "id": "same", "act": "pump", "ms": 1000})
        self.assertEqual([ack["ph"] for ack in answered], ["rejected"])
        self.assertEqual(answered[0]["r"], "duplicate")

    def test_the_ring_forgets_beyond_its_depth(self) -> None:
        """Documented firmware behaviour, so the double must not be kinder.

        A double that remembered every id forever would hide a replay the real
        board would execute twice.
        """

        self.send({"t": "cmd", "id": "oldest", "act": "pump", "ms": 1000})
        for index in range(COMMAND_ID_RING):
            self.send({"t": "cmd", "id": f"filler-{index}", "act": "pump", "ms": 1000})
        answered = self.send({"t": "cmd", "id": "oldest", "act": "pump", "ms": 1000})
        self.assertEqual([ack["ph"] for ack in answered], ["accepted", "completed"])

    def test_the_deadman_tick_is_not_acknowledged(self) -> None:
        self.assertEqual(self.send({"t": "ka"}), [])

    def test_a_command_with_no_id_gets_no_ack(self) -> None:
        """Nothing could correlate the answer, so there is no answer to give."""

        self.assertEqual(self.send({"t": "cmd", "act": "pump", "ms": 1000}), [])

    def test_a_nonsensical_duration_is_rejected_not_run(self) -> None:
        for bad in (0, -5, "18000", None):
            with self.subTest(ms=bad):
                handle = LoopbackArduino(node_id="n", announce_hello=False)
                handle.write(
                    json.dumps({"t": "cmd", "id": "x", "act": "pump", "ms": bad}).encode()
                    + b"\n"
                )
                answered = acks(handle)
                self.assertEqual([ack["ph"] for ack in answered], ["rejected"])

    def test_partial_writes_are_reassembled(self) -> None:
        """A serial link may deliver a frame in pieces."""

        payload = json.dumps({"t": "cmd", "id": "split", "act": "pump", "ms": 1000})
        self.handle.write(payload[:10].encode())
        self.assertEqual(acks(self.handle), [])
        self.handle.write(payload[10:].encode() + b"\n")
        self.assertEqual(
            [ack["ph"] for ack in acks(self.handle)], ["accepted", "completed"]
        )

    def test_an_unparsable_frame_is_recorded_not_raised(self) -> None:
        self.handle.write(b"not json\n")
        self.assertEqual(self.handle.unparsable, [b"not json"])
        self.assertEqual(acks(self.handle), [])

    def test_respond_can_force_any_outcome(self) -> None:
        """The escape hatch. Watchdogs and cooldown timing are not modelled here.

        A second implementation of the interlocks would drift from the firmware,
        and a double that disagrees with the firmware is worse than none.
        """

        handle = LoopbackArduino(
            node_id="n",
            announce_hello=False,
            respond=lambda message: [
                {
                    "t": "ack",
                    "id": message["id"],
                    "ph": "aborted",
                    "ms": 3020,
                    "stop": "watchdog",
                }
            ],
        )
        handle.write(b'{"t":"cmd","id":"c1","act":"pump","ms":18000}\n')
        answered = acks(handle)
        self.assertEqual(answered[0]["ph"], "aborted")
        self.assertEqual(answered[0]["stop"], "watchdog")


class ReadTimeoutTests(unittest.TestCase):
    def test_the_reader_timeout_is_adopted_so_an_idle_loop_does_not_spin(self) -> None:
        """An instantly-empty read turns the bridge's ingest loop into a spin."""

        handle = LoopbackArduino(node_id="n", announce_hello=False)
        self.assertEqual(handle.read_timeout, 0.0)
        reader_for(handle)
        self.assertEqual(handle.read_timeout, 0.0)

        reader_for(handle).lines(Event())  # not advanced: factory not called yet
        handle.as_factory()(port="loopback-0", baudrate=115200, timeout=0.25)
        self.assertEqual(handle.read_timeout, 0.25)


class ThroughTheReaderTests(unittest.TestCase):
    """The double plugged into the real SerialLineReader, both directions."""

    def test_frames_arrive_as_lines_and_commands_go_back(self) -> None:
        handle = LoopbackArduino(node_id="terrabyte-node-01")
        handle.queue_telemetry()
        reader = reader_for(handle)
        generator = reader.lines(Event())

        self.assertEqual(json.loads(next(generator))["message_type"], "hello")
        self.assertEqual(json.loads(next(generator))["message_type"], "telemetry")

        self.assertTrue(
            reader.write_line(b'{"t":"cmd","id":"c1","act":"pump","ms":1000}')
        )
        self.assertEqual(handle.received[0]["id"], "c1")
        self.assertEqual(json.loads(next(generator))["ph"], "accepted")
        self.assertEqual(json.loads(next(generator))["ph"], "completed")
        generator.close()

    def test_the_ring_survives_a_simulated_cable_pull(self) -> None:
        """as_factory hands back one instance, so a reconnect is not amnesia.

        A fresh double on every reconnect would let a replayed command through
        and report the reconnect logic as working.
        """

        handle = LoopbackArduino(node_id="terrabyte-node-01", announce_hello=False)
        reader = reader_for(handle)

        generator = reader.lines(Event())
        handle.queue_telemetry()
        next(generator)
        reader.write_line(b'{"t":"cmd","id":"c1","act":"pump","ms":1000}')
        generator.close()

        generator = reader.lines(Event())
        handle.queue_telemetry()
        next(generator)
        reader.write_line(b'{"t":"cmd","id":"c1","act":"pump","ms":1000}')
        replies = [
            message
            for message in acks(handle)
            if message["id"] == "c1" and message["ph"] == "rejected"
        ]
        self.assertEqual([reply["r"] for reply in replies], ["duplicate"])
        generator.close()


if __name__ == "__main__":
    unittest.main()
