import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from disense.translation import TranslationError, _opencode, _openrouter


class ProviderTests(unittest.TestCase):
    def test_opencode_uses_isolated_agent_and_reads_json_events(self):
        def fake_run(command, **kwargs):
            config = json.loads(kwargs["env"]["OPENCODE_CONFIG_CONTENT"])
            self.assertEqual(config["agents"]["translator"]["permissions"][0]["effect"], "deny")
            self.assertIn("--standalone", command)
            self.assertIn("--format", command)
            self.assertIn("provider/model", command)
            self.assertNotIn("Секретный текст", " ".join(command))
            return SimpleNamespace(returncode=0, stdout=json.dumps({
                "type": "text", "part": {"text": '["Переклад"]'}
            }) + "\n")

        with patch.dict(os.environ, {"OPENCODE_VERSION": "v2"}), patch("disense.translation.subprocess.run", side_effect=fake_run):
            self.assertEqual(_opencode("provider/model", "ru", "uk", ["Секретный текст"]), '["Переклад"]')

    def test_opencode_rejects_bad_model_without_spawning_process(self):
        with patch("disense.translation.subprocess.run") as run:
            with self.assertRaises(TranslationError):
                _opencode("model with spaces", "ru", "uk", ["Text"])
            run.assert_not_called()

    def test_opencode_v1_uses_v1_agent_config(self):
        def fake_run(command, **kwargs):
            config = json.loads(kwargs["env"]["OPENCODE_CONFIG_CONTENT"])
            self.assertEqual(config["agent"]["translator"]["permission"]["*"], "deny")
            self.assertNotIn("--standalone", command)
            return SimpleNamespace(returncode=0, stdout=json.dumps({
                "type": "text", "part": {"text": '["Переклад"]'}
            }) + "\n")

        with patch.dict(os.environ, {"OPENCODE_VERSION": "v1"}), patch("disense.translation.subprocess.run", side_effect=fake_run):
            self.assertEqual(_opencode("provider/model", "ru", "uk", ["Текст"]), '["Переклад"]')

    def test_openrouter_uses_selected_model_and_key_header(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *_): return None
            def read(self, *_):
                return json.dumps({"choices": [{"message": {"content": '["Переклад"]'}}]}).encode()

        def fake_urlopen(request, **kwargs):
            self.assertEqual(json.loads(request.data)["model"], "author/model")
            self.assertEqual(request.get_header("Authorization"), "Bearer test-key")
            return Response()

        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}), patch("disense.translation.urlopen", side_effect=fake_urlopen):
            self.assertEqual(_openrouter("author/model", "ru", "uk", ["Текст"]), '["Переклад"]')


if __name__ == "__main__":
    unittest.main()
