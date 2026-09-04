from __future__ import annotations

import unittest

from quant_agent_harness.harness import _strip_echo_fields
from quant_agent_harness.llm import OpenAICompatibleClient, _parse_json_object


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
        self.assertEqual(captured["max_tokens"], 4096)


class JsonRecoveryTests(unittest.TestCase):
    def test_plain_object_parses(self):
        self.assertEqual(_parse_json_object('{"a": 1}'), {"a": 1})

    def test_fenced_code_block_is_stripped(self):
        self.assertEqual(
            _parse_json_object('```json\n{"a": 1}\n```'),
            {"a": 1},
        )

    def test_trailing_prose_is_dropped(self):
        self.assertEqual(
            _parse_json_object('{"a": {"b": [1, 2]}} 以上是输出'),
            {"a": {"b": [1, 2]}},
        )

    def test_truncated_object_is_recovered_to_the_last_complete_value(self):
        # 模型截断时最常见的形态：最后一个值没写完。补上括号后内层
        # 对象为空但整体完整——比整份报废强得多。
        self.assertEqual(
            _parse_json_object('{"a": 1, "b": {"c": "截断前已完'),
            {"a": 1, "b": {}},
        )

    def test_nested_truncation_recovers_to_a_valid_prefix(self):
        self.assertEqual(
            _parse_json_object('{"summary": "完整", "claims": [{"t": "第一条"}, {"t": "第'),
            {"summary": "完整", "claims": [{"t": "第一条"}, {}]},
        )


class EchoStrippingTests(unittest.TestCase):
    def test_input_metadata_echo_is_stripped_before_validation(self):
        self.assertEqual(
            _strip_echo_fields(
                {
                    "agent_id": "global_market",
                    "task_id": "t-1",
                    "run_id": "r-1",
                    "report_id": "p-1",
                    "generated_at": "2026-08-17 14:16:46",
                    "summary": "真实摘要",
                }
            ),
            {"agent_id": "global_market", "summary": "真实摘要"},
        )

    def test_unknown_fields_survive_for_strict_validation_to_reject(self):
        self.assertIn("brand_new_field", _strip_echo_fields({"brand_new_field": 1}))


if __name__ == "__main__":
    unittest.main()
