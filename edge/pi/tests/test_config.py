from pathlib import Path
import tempfile
import unittest

from terrabyte_edge.config import ConfigError, Settings


# HTTP transport must be requested explicitly; it carries its own required
# settings (base URL, device token) that MQTT-only deployments should never
# have to provide.
BASE_ENV = {
    "TB_TRANSPORT": "http",
    "TB_SERIAL_PORT": "/dev/serial/by-id/usb-test",
    "TB_BACKEND_BASE_URL": "https://api.example.test/",
    "TB_CROP_CONTEXT_ID": "ctx/id",
    "TB_DEVICE_ID": "gateway-1",
    "TB_EXPECTED_NODE_ID": "node-1",
    "TB_DEVICE_TOKEN": "secret",
}

# Minimal MQTT config (the default transport). Deliberately omits
# TB_BACKEND_BASE_URL / TB_DEVICE_TOKEN to prove they are optional here.
MQTT_ENV = {
    "TB_SERIAL_PORT": "/dev/serial/by-id/usb-test",
    "TB_CROP_CONTEXT_ID": "ctx/id",
    "TB_DEVICE_ID": "gateway-1",
    "TB_EXPECTED_NODE_ID": "node-1",
    "TB_MQTT_HOST": "mqtt.example.test",
}


class ConfigTests(unittest.TestCase):
    def test_required_settings_and_telemetry_url(self) -> None:
        settings = Settings.from_env(BASE_ENV)
        self.assertEqual(settings.serial_baud, 115200)
        self.assertEqual(
            settings.telemetry_url(),
            "https://api.example.test/api/telemetry",
        )

    def test_token_file_is_supported_without_exposing_contents(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "token"
            path.write_text("file-secret\n", encoding="utf-8")
            env = dict(BASE_ENV)
            del env["TB_DEVICE_TOKEN"]
            env["TB_DEVICE_TOKEN_FILE"] = str(path)
            self.assertEqual(Settings.from_env(env).device_token, "file-secret")

    def test_exactly_one_token_source_is_required_under_http_transport(self) -> None:
        with self.assertRaises(ConfigError):
            Settings.from_env({key: value for key, value in BASE_ENV.items() if key != "TB_DEVICE_TOKEN"})
        env = dict(BASE_ENV, TB_DEVICE_TOKEN_FILE="/tmp/token")
        with self.assertRaises(ConfigError):
            Settings.from_env(env)

    def test_plain_http_requires_explicit_development_opt_in(self) -> None:
        env = dict(BASE_ENV, TB_BACKEND_BASE_URL="http://127.0.0.1:8080")
        with self.assertRaisesRegex(ConfigError, "TB_ALLOW_INSECURE_HTTP"):
            Settings.from_env(env)
        env["TB_ALLOW_INSECURE_HTTP"] = "true"
        self.assertEqual(
            Settings.from_env(env).backend_base_url,
            "http://127.0.0.1:8080",
        )

    def test_expected_node_id_is_validated(self) -> None:
        with self.assertRaisesRegex(ConfigError, "TB_EXPECTED_NODE_ID"):
            Settings.from_env(dict(BASE_ENV, TB_EXPECTED_NODE_ID="node with space"))

    # --- MQTT transport ---

    def test_mqtt_is_the_default_transport(self) -> None:
        env = {key: value for key, value in MQTT_ENV.items()}
        settings = Settings.from_env(env)
        self.assertEqual(settings.transport, "mqtt")

    def test_mqtt_defaults(self) -> None:
        settings = Settings.from_env(MQTT_ENV)
        self.assertEqual(settings.mqtt_host, "mqtt.example.test")
        self.assertEqual(settings.mqtt_port, 1883)
        self.assertEqual(settings.mqtt_topic_prefix, "tb/v2")
        self.assertEqual(settings.mqtt_keepalive_seconds, 30)
        self.assertEqual(settings.mqtt_publish_timeout_seconds, 10.0)
        self.assertFalse(settings.mqtt_tls)
        self.assertIsNone(settings.mqtt_username)
        self.assertIsNone(settings.mqtt_password)
        self.assertEqual(
            settings.mqtt_telemetry_topic(), "tb/v2/gateway-1/up/telemetry"
        )
        self.assertEqual(settings.mqtt_status_topic(), "tb/v2/gateway-1/up/status")

    def test_http_settings_are_optional_under_mqtt_transport(self) -> None:
        settings = Settings.from_env(MQTT_ENV)
        self.assertEqual(settings.backend_base_url, "")
        self.assertEqual(settings.device_token, "")

    def test_mqtt_requires_host(self) -> None:
        env = {key: value for key, value in MQTT_ENV.items() if key != "TB_MQTT_HOST"}
        with self.assertRaisesRegex(ConfigError, "TB_MQTT_HOST"):
            Settings.from_env(env)

    def test_mqtt_topic_prefix_is_customizable_and_trailing_slash_is_stripped(
        self,
    ) -> None:
        env = dict(MQTT_ENV, TB_MQTT_TOPIC_PREFIX="tb/v3/")
        self.assertEqual(Settings.from_env(env).mqtt_topic_prefix, "tb/v3")

    def test_mqtt_credentials_are_optional(self) -> None:
        env = dict(
            MQTT_ENV, TB_MQTT_USERNAME="gw-gateway-1", TB_MQTT_PASSWORD="hunter2"
        )
        settings = Settings.from_env(env)
        self.assertEqual(settings.mqtt_username, "gw-gateway-1")
        self.assertEqual(settings.mqtt_password, "hunter2")

    def test_invalid_transport_is_rejected(self) -> None:
        env = dict(MQTT_ENV, TB_TRANSPORT="carrier-pigeon")
        with self.assertRaisesRegex(ConfigError, "TB_TRANSPORT"):
            Settings.from_env(env)


class PotConfigTests(unittest.TestCase):
    """Pot volume and crop, the two physical facts the edge is authoritative for.

    Both are keyed by node id rather than positional so a reordering can never
    hand one pot's settings to another.
    """

    def test_both_settings_are_optional(self) -> None:
        settings = Settings.from_env(MQTT_ENV)
        self.assertEqual(settings.pot_substrate_ml, {})
        self.assertEqual(settings.pot_crop_codes, {})
        self.assertIsNone(settings.substrate_volume_ml_for("node-1"))
        self.assertIsNone(settings.crop_code_for("node-1"))

    def test_node_keyed_entries_are_parsed(self) -> None:
        settings = Settings.from_env(
            dict(
                MQTT_ENV,
                TB_POT_SUBSTRATE_ML="node-1:3000",
                TB_POT_CROPS="node-1:lettuce",
            )
        )
        self.assertEqual(settings.substrate_volume_ml_for("node-1"), 3000)
        self.assertEqual(settings.crop_code_for("node-1"), "lettuce")

    def test_surrounding_whitespace_is_tolerated(self) -> None:
        settings = Settings.from_env(
            dict(MQTT_ENV, TB_POT_SUBSTRATE_ML=" node-1 : 1500 ")
        )
        self.assertEqual(settings.substrate_volume_ml_for("node-1"), 1500)

    def test_a_node_id_this_gateway_does_not_serve_is_rejected(self) -> None:
        """A typo here would size doses for a pot that is not plugged in.

        Ignoring the entry would look identical to having configured nothing,
        so the mistake has to stop the service instead.
        """

        for name in ("TB_POT_SUBSTRATE_ML", "TB_POT_CROPS"):
            value = "node-2:3000" if name.endswith("ML") else "node-2:lettuce"
            with self.subTest(setting=name):
                with self.assertRaisesRegex(ConfigError, "node-2"):
                    Settings.from_env(dict(MQTT_ENV, **{name: value}))

    def test_an_unknown_crop_code_is_rejected(self) -> None:
        with self.assertRaisesRegex(ConfigError, "durian"):
            Settings.from_env(dict(MQTT_ENV, TB_POT_CROPS="node-1:durian"))

    def test_a_node_id_containing_a_colon_still_parses(self) -> None:
        """Node ids may contain ':', so the split has to be on the last one."""

        env = dict(
            MQTT_ENV,
            TB_EXPECTED_NODE_ID="node_A-1.2:usb",
            TB_POT_SUBSTRATE_ML="node_A-1.2:usb:1500",
        )
        self.assertEqual(
            Settings.from_env(env).substrate_volume_ml_for("node_A-1.2:usb"), 1500
        )

    def test_malformed_entries_are_rejected(self) -> None:
        for value in ("node-1", "3000", ":3000", "node-1:"):
            with self.subTest(value=value):
                with self.assertRaises(ConfigError):
                    Settings.from_env(dict(MQTT_ENV, TB_POT_SUBSTRATE_ML=value))

    def test_volumes_must_be_positive_whole_millilitres(self) -> None:
        for value in ("node-1:0", "node-1:-500", "node-1:3.5", "node-1:big"):
            with self.subTest(value=value):
                with self.assertRaises(ConfigError):
                    Settings.from_env(dict(MQTT_ENV, TB_POT_SUBSTRATE_ML=value))

    def test_a_repeated_node_id_is_rejected_rather_than_last_wins(self) -> None:
        with self.assertRaisesRegex(ConfigError, "twice"):
            Settings.from_env(
                dict(MQTT_ENV, TB_POT_SUBSTRATE_ML="node-1:3000,node-1:1500")
            )


class MultiNodeConfigTests(unittest.TestCase):
    """A gateway fronting up to four Arduinos.

    Existing boards already have the singular variables in
    /etc/terrabyte-edge.env, so both spellings must keep working.
    """

    def test_singular_variables_still_work(self) -> None:
        settings = Settings.from_env(MQTT_ENV)
        self.assertEqual(settings.serial_ports, ("/dev/serial/by-id/usb-test",))
        self.assertEqual(settings.expected_node_ids, frozenset({"node-1"}))

    def test_plural_variables_are_parsed(self) -> None:
        settings = Settings.from_env(
            dict(
                MQTT_ENV,
                TB_SERIAL_PORTS="/dev/a, /dev/b ,/dev/c",
                TB_EXPECTED_NODE_IDS="node-1,node-2,node-3",
            )
        )
        self.assertEqual(settings.serial_ports, ("/dev/a", "/dev/b", "/dev/c"))
        self.assertEqual(
            settings.expected_node_ids, frozenset({"node-1", "node-2", "node-3"})
        )

    def test_plural_wins_over_singular(self) -> None:
        """A half-finished edit must not leave the plural list silently ignored."""

        settings = Settings.from_env(
            dict(
                MQTT_ENV,
                TB_SERIAL_PORTS="/dev/a,/dev/b",
                TB_EXPECTED_NODE_IDS="node-1,node-2",
            )
        )
        self.assertEqual(settings.serial_ports, ("/dev/a", "/dev/b"))

    def test_more_than_four_ports_is_rejected(self) -> None:
        with self.assertRaisesRegex(ConfigError, "at most 4"):
            Settings.from_env(
                dict(
                    MQTT_ENV,
                    TB_SERIAL_PORTS="/dev/a,/dev/b,/dev/c,/dev/d,/dev/e",
                    TB_EXPECTED_NODE_IDS="n1,n2,n3,n4",
                )
            )

    def test_duplicate_entries_are_rejected(self) -> None:
        with self.assertRaisesRegex(ConfigError, "duplicates"):
            Settings.from_env(dict(MQTT_ENV, TB_SERIAL_PORTS="/dev/a,/dev/a"))

    def test_more_ports_than_nodes_is_rejected(self) -> None:
        with self.assertRaisesRegex(ConfigError, "more entries"):
            Settings.from_env(
                dict(MQTT_ENV, TB_SERIAL_PORTS="/dev/a,/dev/b,/dev/c")
            )

    def test_fewer_ports_than_nodes_is_allowed(self) -> None:
        """Cabled for four pots, only two Arduinos powered on."""

        settings = Settings.from_env(
            dict(
                MQTT_ENV,
                TB_SERIAL_PORTS="/dev/a,/dev/b",
                TB_EXPECTED_NODE_IDS="n1,n2,n3,n4",
            )
        )
        self.assertEqual(len(settings.serial_ports), 2)
        self.assertEqual(len(settings.expected_node_ids), 4)

    def test_node_id_characters_are_validated(self) -> None:
        with self.assertRaisesRegex(ConfigError, "unsupported characters"):
            Settings.from_env(
                dict(MQTT_ENV, TB_EXPECTED_NODE_IDS="node-1,node with space")
            )

    def test_claim_code_must_be_six_digits(self) -> None:
        with self.assertRaisesRegex(ConfigError, "six digits"):
            Settings.from_env(dict(MQTT_ENV, TB_CLAIM_CODE="48392"))
        self.assertEqual(
            Settings.from_env(dict(MQTT_ENV, TB_CLAIM_CODE="483920")).claim_code,
            "483920",
        )

    def test_claim_code_is_optional(self) -> None:
        self.assertEqual(Settings.from_env(MQTT_ENV).claim_code, "")

    def test_snapshot_defaults_to_run_directory(self) -> None:
        settings = Settings.from_env(MQTT_ENV)
        self.assertEqual(
            settings.status_snapshot_path,
            Path("/run/terrabyte-edge/status.json"),
        )


if __name__ == "__main__":
    unittest.main()
