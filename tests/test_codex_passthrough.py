import asyncio
import copy
import importlib.util
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from LiteLLM.runtime.codex_passthrough import CODEX_PASSTHROUGH, RESPONSES_CALL_TYPES, strip_codex_passthrough


CALLBACK = "LiteLLM.runtime.callbacks.codex_passthrough"


def message(metadata=None, text="hi"):
    item = {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}
    if metadata is not None:
        item[CODEX_PASSTHROUGH] = metadata
    return item


def codex_request():
    return {
        "model": "gpt-6-luna",
        "stream": True,
        "litellm_metadata": {"model_group": "coding"},
        "input": [
            message({"turn_id": "t1", "content_item_kinds": ["input_text"], "content_item_metadata": [{}]}),
            {"type": "function_call_output", "call_id": "c1", "output": "ok", CODEX_PASSTHROUGH: {"turn_id": "t1"}},
            message(),
        ],
    }


class CodexPassthroughTests(unittest.TestCase):
    def test_field_is_removed_from_every_item_on_all_responses_routes(self):
        for route in sorted(RESPONSES_CALL_TYPES):
            with self.subTest(route=route):
                request = codex_request()
                result = strip_codex_passthrough(request, route)
                self.assertIsNotNone(result)
                self.assertFalse(any(CODEX_PASSTHROUGH in item for item in result["input"]))
                expected = codex_request()
                for item in expected["input"]:
                    item.pop(CODEX_PASSTHROUGH, None)
                self.assertEqual(result, expected)

    def test_call_type_enum_values_are_accepted(self):
        route = SimpleNamespace(value="aresponses")
        result = strip_codex_passthrough(codex_request(), route)
        self.assertFalse(any(CODEX_PASSTHROUGH in item for item in result["input"]))

    def test_compact_without_call_type_uses_the_logging_call_type(self):
        request = codex_request()
        request["litellm_logging_obj"] = logging = SimpleNamespace(call_type="acompact_responses")
        result = strip_codex_passthrough(request, None)
        self.assertFalse(any(CODEX_PASSTHROUGH in item for item in result["input"]))
        self.assertIs(result["litellm_logging_obj"], logging)

    def test_caller_request_and_items_are_not_mutated(self):
        request = codex_request()
        original = copy.deepcopy(request)
        items = list(request["input"])
        result = strip_codex_passthrough(request, "aresponses")
        self.assertEqual(request, original)
        self.assertTrue(all(current is before for current, before in zip(request["input"], items)))
        self.assertIsNot(result, request)
        self.assertIsNot(result["input"], request["input"])
        result["input"][0]["content"][0]["text"] = "changed"
        result["input"][1]["output"] = "changed"
        self.assertEqual(request["input"], original["input"])

    def test_non_responses_routes_are_untouched(self):
        for route in ("acompletion", "completion", "aembedding", "_aresponses_websocket", "aget_responses", "unknown", 7, None):
            with self.subTest(route=route):
                request = codex_request()
                self.assertIsNone(strip_codex_passthrough(request, route))
                self.assertEqual(request, codex_request())
        request = codex_request()
        request["litellm_logging_obj"] = SimpleNamespace(call_type="acompletion")
        self.assertIsNone(strip_codex_passthrough(request, None))

    def test_requests_without_the_field_return_none(self):
        for request in (
            {"input": [message(), "raw text", {"type": "reasoning", "encrypted_content": "e"}]},
            {"input": "plain prompt"},
            {"input": []},
            {"input": None},
            {"model": "gpt-6-luna"},
            {"input": ({CODEX_PASSTHROUGH: {}},)},
            None,
            [message({"content_item_metadata": []})],
        ):
            with self.subTest(request=request):
                self.assertIsNone(strip_codex_passthrough(request, "aresponses"))

    def test_non_mapping_items_are_preserved_in_order(self):
        marker = SimpleNamespace(kind="opaque")
        request = {"input": ["raw text", message({"content_item_metadata": [1]}), ["nested"], marker, 3]}
        result = strip_codex_passthrough(request, "responses")
        self.assertEqual(result["input"][0], "raw text")
        self.assertEqual(result["input"][1], message())
        self.assertEqual(result["input"][2], ["nested"])
        self.assertIsNot(result["input"][2], request["input"][2])
        self.assertEqual(result["input"][3].kind, "opaque")
        self.assertEqual(result["input"][4], 3)
        self.assertEqual(len(result["input"]), 5)


@unittest.skipUnless(importlib.util.find_spec("litellm") or os.environ.get("RUN_RUNTIME_CALLBACK_TESTS") == "1",
                     "LiteLLM is installed in the reviewed runtime image")
class CodexPassthroughCallbackTests(unittest.TestCase):
    def load_callback(self):
        from litellm.proxy.types_utils.utils import get_instance_fn

        with tempfile.TemporaryDirectory() as directory:
            # Resolve the name exactly as the proxy does for /app/config/config.yaml.
            return get_instance_fn(value=CALLBACK, config_file_path=os.path.join(directory, "config.yaml"))

    def test_registered_name_loads_a_custom_logger(self):
        from litellm.integrations.custom_logger import CustomLogger

        hook = self.load_callback()
        self.assertIsInstance(hook, CustomLogger)
        self.assertEqual(type(hook).__name__, "CodexPassthroughCompatibility")

    def test_litellm_dispatch_strips_responses_and_ignores_other_calls(self):
        import litellm
        from litellm.utils import async_pre_call_deployment_hook

        hook = self.load_callback()
        with patch.object(litellm, "callbacks", [hook]):
            request = codex_request()
            result = asyncio.run(async_pre_call_deployment_hook(request, "aresponses"))
            self.assertFalse(any(CODEX_PASSTHROUGH in item for item in result["input"]))
            self.assertEqual(request, codex_request())

            compact = codex_request()
            compact["litellm_logging_obj"] = SimpleNamespace(call_type="acompact_responses")
            result = asyncio.run(async_pre_call_deployment_hook(compact, "acompact_responses"))
            self.assertFalse(any(CODEX_PASSTHROUGH in item for item in result["input"]))

            chat = codex_request()
            result = asyncio.run(async_pre_call_deployment_hook(chat, "acompletion"))
            self.assertEqual(result, codex_request())


if __name__ == "__main__":
    unittest.main()
