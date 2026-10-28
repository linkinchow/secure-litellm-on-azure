"""LiteLLM callbacks shipped in the reviewed Azure runtime image.

The Stage 6 backend renderer registers them in ``litellm_settings.callbacks``.
LiteLLM 1.104.0 imports a dotted callback name with importlib when no file of
that path exists beside /app/config/config.yaml; the image sets PYTHONPATH=/opt/llmgw.
"""

from litellm.integrations.custom_logger import CustomLogger

from LiteLLM.runtime.codex_passthrough import strip_codex_passthrough


class CodexPassthroughCompatibility(CustomLogger):
    """Request-only Responses compatibility; it logs, stores and exports nothing."""

    async def async_pre_call_deployment_hook(self, kwargs, call_type):
        return strip_codex_passthrough(kwargs, call_type)


codex_passthrough = CodexPassthroughCompatibility()
