from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from quant_agent_harness.repository import Repository
from quant_agent_harness.server import ProtocolServer


class SettingsTests(unittest.TestCase):
    def test_secret_roundtrip_is_encrypted_at_rest(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "settings.sqlite"
            repository = Repository(database)
            repository.set_secret("test.key", "plain-secret-value")
            self.assertEqual(repository.get_secret("test.key"), "plain-secret-value")
            connection = sqlite3.connect(database)
            try:
                encrypted = bytes(
                    connection.execute(
                        "SELECT encrypted_value FROM secrets WHERE secret_key='test.key'"
                    ).fetchone()[0]
                )
            finally:
                connection.close()
            self.assertNotIn(b"plain-secret-value", encrypted)

    def test_protocol_saves_redacted_settings_and_reloads_model(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = Repository(Path(temp_dir) / "settings.sqlite")
            server = ProtocolServer(repository)
            result = server.handle(
                "save_settings",
                {
                    "model": {
                        "base_url": "https://model.example/v1",
                        "model": "example-model",
                        "api_key": "secret-model-key",
                    },
                    "tushare": {"token": "secret-tushare-token"},
                    "tavily": {"api_key": "secret-tavily-key"},
                },
            )
            settings = result["settings"]
            self.assertTrue(settings["model"]["api_key_configured"])
            self.assertTrue(settings["model"]["ready"])
            self.assertTrue(settings["tushare"]["token_configured"])
            self.assertNotIn("secret-model-key", str(settings))
            self.assertEqual(server.harness.llm_client.model, "example-model")
            self.assertIsNotNone(server.harness.tushare_client)
            self.assertIsNotNone(server.harness.tavily_client)

            cleared = server.handle(
                "save_settings",
                {
                    "model": {"base_url": "https://model.example/v1", "model": "example-model"},
                    "clear_secrets": ["model.api_key"],
                },
            )["settings"]
            self.assertFalse(cleared["model"]["api_key_configured"])
            self.assertFalse(cleared["model"]["ready"])


if __name__ == "__main__":
    unittest.main()
