"""The provisioning tool's one job is that three files agree.

Everything else it does (random codes, file modes, refusing to overwrite) only
matters because a disagreement between the env, the manifest and the backend
row is silent: the wizard shows a confident registration number and the user
claims someone else's gateway.
"""

import io
import json
import stat
import sys
import tempfile
import unittest
from pathlib import Path

# tools/ is not a package and edge/pi has no packaging at all, so the tool has
# to be put on sys.path by hand. Same trick tools/train_irrigation_rf.py uses
# for its own sibling imports; preferred over importlib here because the plain
# import statement below then reads like any other test in this suite.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import provision  # noqa: E402


def parse_env(text: str) -> dict:
    """Parse the fragment the way a shell / systemd EnvironmentFile would."""

    values = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        assert separator, f"KEY=value 형식이 아닙니다: {line!r}"
        values[key] = value
    return values


class ProvisionTestCase(unittest.TestCase):
    """Every test wants a throwaway output directory."""

    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name)
        self.out = self.root / "out"

    def tearDown(self) -> None:
        self._directory.cleanup()

    def run_tool(self, *args: str):
        """Run main() with captured streams, like the CLI would be run."""

        stdout, stderr = io.StringIO(), io.StringIO()
        code = provision.main(list(args), stdout=stdout, stderr=stderr)
        return code, stdout.getvalue(), stderr.getvalue()

    def provision_gateway(self, *extra: str, device_id="orangepi-pro-03"):
        code, stdout, stderr = self.run_tool(
            "--device-id", device_id, "--output-dir", str(self.out), *extra
        )
        self.assertEqual(code, 0, stderr)
        return stdout

    def manifest(self) -> dict:
        return json.loads((self.out / "provisioning.json").read_text(encoding="utf-8"))

    def env(self) -> dict:
        return parse_env((self.out / "terrabyte-edge.env").read_text(encoding="utf-8"))

    def sql(self) -> str:
        return (self.out / "backend.sql").read_text(encoding="utf-8")


class OutputTests(ProvisionTestCase):
    def test_all_three_files_are_written_and_parse(self) -> None:
        self.provision_gateway()
        manifest = self.manifest()
        self.assertEqual(manifest["device_id"], "orangepi-pro-03")
        env = self.env()
        self.assertEqual(
            set(env),
            {"TB_DEVICE_ID", "TB_CLAIM_CODE", "TB_MQTT_USERNAME", "TB_MQTT_PASSWORD"},
        )
        self.assertIn("INSERT INTO device", self.sql())

    def test_manifest_keys_match_what_the_wizard_reads(self) -> None:
        """identity.verify_identity reads device_id and claim_code by name."""

        self.provision_gateway()
        self.assertEqual(
            set(self.manifest()),
            {"device_id", "claim_code", "mqtt_username", "provisioned_at"},
        )

    def test_provisioned_at_is_iso_8601_utc(self) -> None:
        self.provision_gateway()
        stamp = self.manifest()["provisioned_at"]
        self.assertTrue(stamp.endswith("Z"), stamp)
        self.assertEqual(len(stamp), len("2026-08-01T09:30:00Z"))
        self.assertEqual(stamp[4], "-")
        self.assertEqual(stamp[10], "T")

    def test_one_identity_appears_in_all_three_outputs(self) -> None:
        """The whole point of the tool. If these ever drift, the box shows a
        number that claims another user's gateway."""

        self.provision_gateway(device_id="orangepi-pro-07")
        manifest, env, sql = self.manifest(), self.env(), self.sql()

        self.assertEqual(manifest["device_id"], env["TB_DEVICE_ID"])
        self.assertEqual(manifest["claim_code"], env["TB_CLAIM_CODE"])
        self.assertEqual(manifest["mqtt_username"], env["TB_MQTT_USERNAME"])

        self.assertIn(f"'{manifest['device_id']}'", sql)
        self.assertIn(f"'{manifest['claim_code']}'", sql)
        self.assertIn(f"'{manifest['mqtt_username']}'", sql)

    def test_mqtt_username_follows_the_backend_convention(self) -> None:
        """V7__relax_device_ownership.sql built every existing username as
        'gw-' || hardware_id; a different rule here would fail to match."""

        self.provision_gateway(device_id="orangepi-pro-11")
        self.assertEqual(self.manifest()["mqtt_username"], "gw-orangepi-pro-11")
        self.assertEqual(self.env()["TB_MQTT_USERNAME"], "gw-orangepi-pro-11")

    def test_sql_is_idempotent(self) -> None:
        """An operator who is unsure whether it landed must be able to re-run
        it rather than guess."""

        self.provision_gateway()
        sql = self.sql()
        self.assertIn("ON CONFLICT (hardware_id) DO UPDATE SET", sql)
        self.assertIn("claim_code = EXCLUDED.claim_code", sql)
        self.assertIn("mqtt_username = EXCLUDED.mqtt_username", sql)

    def test_password_hash_reaches_the_sql_only_when_supplied(self) -> None:
        """No stdlib bcrypt, so an unsupplied hash must stay unset rather than
        become a placeholder that looks provisioned but authenticates nobody."""

        self.provision_gateway()
        self.assertNotIn("mqtt_password_hash = EXCLUDED", self.sql())

        second = self.root / "out2"
        code, _, stderr = self.run_tool(
            "--device-id", "orangepi-pro-04",
            "--output-dir", str(second),
            "--mqtt-password-hash", "{bcrypt}$2a$10$abcdef",
        )
        self.assertEqual(code, 0, stderr)
        sql = (second / "backend.sql").read_text(encoding="utf-8")
        self.assertIn("'{bcrypt}$2a$10$abcdef'", sql)
        self.assertIn("mqtt_password_hash = EXCLUDED.mqtt_password_hash", sql)


class ClaimCodeTests(ProvisionTestCase):
    def test_generated_code_is_always_six_ascii_digits(self) -> None:
        """Generated many times rather than seeded: secrets is deliberately
        unseedable, and the invariant is what matters, not a fixed value."""

        for _ in range(2000):
            code = provision.generate_claim_code()
            self.assertEqual(len(code), 6, code)
            self.assertTrue(code.isdigit() and code.isascii(), code)

    def test_leading_zero_codes_survive_every_output(self) -> None:
        """'004821' formatted as an int becomes 4821, fails the backend's
        CHAR_LENGTH = 6 check, and no longer matches what the user types."""

        self.provision_gateway("--claim-code", "004821")
        self.assertEqual(self.manifest()["claim_code"], "004821")
        self.assertEqual(self.env()["TB_CLAIM_CODE"], "004821")
        self.assertIn("'004821'", self.sql())

    def test_generator_can_produce_a_leading_zero_code(self) -> None:
        """A generator that never emits one is silently drawing from 100000..
        instead of 000000.., which is a tenth of the keyspace missing."""

        codes = [provision.generate_claim_code() for _ in range(5000)]
        self.assertTrue(any(code.startswith("0") for code in codes))

    def test_bad_claim_codes_are_refused(self) -> None:
        for bad in ("12345", "1234567", "12345a", ""):
            with self.subTest(bad=bad):
                code, _, stderr = self.run_tool(
                    "--device-id", "orangepi-pro-03",
                    "--output-dir", str(self.root / f"out-{bad or 'empty'}"),
                    "--claim-code", bad,
                )
                self.assertEqual(code, 2)
                self.assertIn("claim code", stderr)

    def test_non_ascii_digits_are_refused(self) -> None:
        """str.isdigit() accepts these; a phone keypad cannot type them."""

        with self.assertRaises(provision.ProvisionError):
            provision.validate_claim_code("１２３４５６")


class DeviceIdTests(ProvisionTestCase):
    def test_device_id_that_could_break_the_sql_is_refused(self) -> None:
        code, _, stderr = self.run_tool(
            "--device-id", "gw'; DROP TABLE device; --",
            "--output-dir", str(self.out),
        )
        self.assertEqual(code, 2)
        self.assertIn("device id", stderr)
        self.assertFalse(self.out.exists())


class ForceTests(ProvisionTestCase):
    def test_refuses_to_overwrite_without_force(self) -> None:
        """Regenerating an identity in place is how two boxes end up sharing a
        claim code, with only one of them reachable."""

        self.provision_gateway("--claim-code", "111111")
        code, _, stderr = self.run_tool(
            "--device-id", "orangepi-pro-03", "--output-dir", str(self.out)
        )
        self.assertEqual(code, 2)
        self.assertIn("--force", stderr)
        self.assertEqual(self.manifest()["claim_code"], "111111")

    def test_force_overwrites_including_the_read_only_manifest(self) -> None:
        self.provision_gateway("--claim-code", "111111")
        self.provision_gateway("--claim-code", "222222", "--force")
        self.assertEqual(self.manifest()["claim_code"], "222222")
        self.assertEqual(self.env()["TB_CLAIM_CODE"], "222222")
        self.assertIn("'222222'", self.sql())

    def test_an_empty_directory_is_not_treated_as_occupied(self) -> None:
        """mkdir -p before running is normal; there is no identity to destroy."""

        self.out.mkdir(parents=True)
        self.provision_gateway()
        self.assertTrue((self.out / "provisioning.json").is_file())


class SecrecyTests(ProvisionTestCase):
    def test_file_modes(self) -> None:
        self.provision_gateway()
        env_mode = stat.S_IMODE((self.out / "terrabyte-edge.env").stat().st_mode)
        manifest_mode = stat.S_IMODE((self.out / "provisioning.json").stat().st_mode)
        self.assertEqual(oct(env_mode), oct(0o600))
        self.assertEqual(oct(manifest_mode), oct(0o444))

    def test_summary_never_echoes_the_mqtt_password(self) -> None:
        """It is already in the env file; printing it puts it in terminal
        scrollback and in any CI log that captured the run."""

        password = "correct-horse-battery-staple-Zx9"
        stdout = self.provision_gateway("--mqtt-password", password)
        self.assertNotIn(password, stdout)
        self.assertEqual(self.env()["TB_MQTT_PASSWORD"], password)

    def test_generated_password_is_not_in_the_summary_either(self) -> None:
        stdout = self.provision_gateway()
        self.assertNotIn(self.env()["TB_MQTT_PASSWORD"], stdout)

    def test_password_never_reaches_the_manifest_or_sql(self) -> None:
        """The manifest is world-readable (0444) and the SQL gets pasted into
        chat with an operator."""

        password = "s3cret-password-value"
        self.provision_gateway("--mqtt-password", password)
        self.assertNotIn(password, json.dumps(self.manifest()))
        self.assertNotIn(password, self.sql())

    def test_generated_passwords_are_long_and_distinct(self) -> None:
        passwords = {provision.generate_mqtt_password() for _ in range(200)}
        self.assertEqual(len(passwords), 200)
        self.assertTrue(all(len(password) >= 32 for password in passwords))

    def test_summary_still_shows_the_operator_what_they_need(self) -> None:
        stdout = self.provision_gateway(device_id="orangepi-pro-09")
        self.assertIn("orangepi-pro-09", stdout)
        self.assertIn(self.manifest()["claim_code"], stdout)
        self.assertIn("gw-orangepi-pro-09", stdout)


if __name__ == "__main__":
    unittest.main()
