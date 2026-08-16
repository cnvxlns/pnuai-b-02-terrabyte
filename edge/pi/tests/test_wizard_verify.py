"""Identity verification.

Lives in terrabyte_edge.identity rather than the UI package precisely so it
can be tested on a machine with no Tk installed.

The failure this guards against is silent: a cloned SD image displays a
confident, wrong registration number and the user claims someone else's
gateway. So the rule is that a mismatch must never fall through to the code
screen, while merely being offline must never block a demo.
"""

import json
import tempfile
import unittest
from pathlib import Path

from terrabyte_edge.identity import verify_identity


class VerifyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.manifest = Path(self._directory.name) / "provisioning.json"

    def tearDown(self) -> None:
        self._directory.cleanup()

    def write_manifest(self, device_id: str, claim_code: str) -> None:
        self.manifest.write_text(
            json.dumps({"device_id": device_id, "claim_code": claim_code}),
            encoding="utf-8",
        )

    def verify(self, *, device_id="orangepi-pro-01", claim_code="483920", online=True):
        return verify_identity(
            device_id=device_id,
            claim_code=claim_code,
            manifest_path=self.manifest,
            online=online,
        )

    def test_matching_config_online_is_verified(self) -> None:
        self.write_manifest("orangepi-pro-01", "483920")
        self.assertEqual(self.verify().state, "verified")

    def test_wrong_gateway_id_fails(self) -> None:
        """The cloned-image case."""

        self.write_manifest("orangepi-pro-03", "483920")
        verdict = self.verify(device_id="orangepi-pro-07")
        self.assertEqual(verdict.state, "failed")
        self.assertEqual(verdict.actual, "orangepi-pro-07")
        self.assertEqual(verdict.expected, "orangepi-pro-03")

    def test_wrong_claim_code_fails(self) -> None:
        self.write_manifest("orangepi-pro-01", "771204")
        verdict = self.verify(claim_code="483920")
        self.assertEqual(verdict.state, "failed")
        self.assertEqual(verdict.expected, "771204")

    def test_missing_claim_code_fails(self) -> None:
        """Showing nothing is better than showing a blank the user might read
        as a real value."""

        self.write_manifest("orangepi-pro-01", "483920")
        self.assertEqual(self.verify(claim_code="").state, "failed")

    def test_offline_is_unverified_not_failed(self) -> None:
        """Blocking the demo because the venue Wi-Fi is down would be worse
        than the risk being mitigated."""

        self.write_manifest("orangepi-pro-01", "483920")
        verdict = self.verify(online=False)
        self.assertEqual(verdict.state, "unverified")

    def test_offline_still_catches_a_local_mismatch(self) -> None:
        """The manifest comparison needs no network, so being offline must not
        turn a real mismatch into a pass."""

        self.write_manifest("orangepi-pro-03", "483920")
        self.assertEqual(
            self.verify(device_id="orangepi-pro-07", online=False).state, "failed"
        )

    def test_missing_manifest_is_unverified(self) -> None:
        self.assertEqual(self.verify().state, "unverified")

    def test_corrupt_manifest_is_unverified_not_a_crash(self) -> None:
        self.manifest.write_text("{not json", encoding="utf-8")
        self.assertEqual(self.verify().state, "unverified")

    def test_verdict_never_leaks_a_pass_on_error(self) -> None:
        """Whatever goes wrong, the only way to reach 'verified' is an actual
        match. This is the invariant the red screen depends on."""

        for manifest_body in ("{not json", "", "[]", '{"device_id": null}'):
            self.manifest.write_text(manifest_body, encoding="utf-8")
            self.assertNotEqual(self.verify().state, "verified", manifest_body)


if __name__ == "__main__":
    unittest.main()
