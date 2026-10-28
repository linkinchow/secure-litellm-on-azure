"""Remove Codex's client-only Responses metadata before a request reaches Azure OpenAI.

Codex (CLI and IDE extension, 0.164 and later) attaches
``internal_chat_message_metadata_passthrough`` to every Responses ``input`` item.
Azure OpenAI accepts the object with its older sub-keys but rejects the newer
``content_item_metadata`` sub-key with 400 ``unknown_parameter``. LiteLLM forwards
input items unchanged and ``drop_params`` does not inspect them. Codex keeps its
own copy, so omitting the object upstream does not change client behaviour.

This module has no LiteLLM dependency; the callback registered in config.yaml is
``LiteLLM.runtime.callbacks.codex_passthrough``.
"""

from collections.abc import Mapping
from copy import deepcopy


CODEX_PASSTHROUGH = "internal_chat_message_metadata_passthrough"
RESPONSES_CALL_TYPES = frozenset({"responses", "aresponses", "compact_responses", "acompact_responses"})


def _name(call_type):
    return getattr(call_type, "value", call_type)


def responses_route(kwargs, call_type):
    """Return whether the hook runs for a Responses create or compact call.

    LiteLLM 1.104.0 has no CallTypes member for compact, so the hook receives
    None there; the call type recorded on the request's logging object is used instead.
    """
    route = _name(call_type)
    if route is None:
        route = _name(getattr(kwargs.get("litellm_logging_obj"), "call_type", None))
    return isinstance(route, str) and route in RESPONSES_CALL_TYPES


def strip_codex_passthrough(kwargs, call_type):
    """Return a request copy without the Codex field, or None when nothing changes.

    The caller's request and input items are never modified.
    """
    if not isinstance(kwargs, Mapping) or not responses_route(kwargs, call_type):
        return None
    items = kwargs.get("input")
    if not isinstance(items, list) or not any(isinstance(item, Mapping) and CODEX_PASSTHROUGH in item for item in items):
        return None
    return {**kwargs, "input": [
        {key: deepcopy(value) for key, value in item.items() if key != CODEX_PASSTHROUGH}
        if isinstance(item, Mapping) else deepcopy(item)
        for item in items
    ]}
