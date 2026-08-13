from __future__ import annotations

import unittest

from quant_agent_harness.llm import OpenAICompatibleClient


class LLMClientTests(unittest.TestCase):
    def test_deepseek_v4_uses_fast_bounded_json_mode(self):
        client = OpenAICompatibleClient(
            "https://api.deepseek.com",
            "test-key",
            "deepseek-v4-pro",
        )
        captured: dict = {}

        def fake_post(payload: dict) -> dict:
            captured.update(payload)
            return {
                "model": "deepseek-v4-pro",
                "choices": [{"message": {"content": '{"action":"finish"}'}}],
            }

        client._post = fake_post  # type: ignore[method-assign]
        result = client.complete_json("现在不是做最终总结，只输出 JSON", "{}")

        self.assertEqual(result.data["action"], "finish")
        self.assertEqual(captured["thinking"], {"type": "disabled"})
        self.assertEqual(captured["max_tokens"], 800)

    def test_other_compatible_provider_does_not_receive_deepseek_thinking_option(self):
        client = OpenAICompatibleClient(
            "https://example.com/v1",
            "test-key",
            "some-chat-model",
        )
        captured: dict = {}

        def fake_post(payload: dict) -> dict:
            captured.update(payload)
            return {"choices": [{"message": {"content": "{}"}}]}

        client._post = fake_post  # type: ignore[method-assign]
        client.complete_json("普通结构化任务", "{}")

        self.assertNotIn("thinking", captured)
        self.assertEqual(captured["max_tokens"], 1800)


if __name__ == "__main__":
    unittest.main()
