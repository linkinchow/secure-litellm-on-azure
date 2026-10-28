"""Closed, non-secret runtime override subset for public LiteLLM v1.104.8.

Pinned source: https://github.com/BerriAI/litellm/blob/v1.104.8/
  litellm/proxy/_types.py (GeneralSettings)
  litellm/proxy/proxy_server.py (ProxyConfig.load_config)
  litellm/__init__.py, litellm/constants.py, litellm/_logging.py (globals)
  litellm/router.py (Router.__init__), litellm/types/litellm_params.py (strategies)

This is deliberately narrower than LiteLLM's configuration API. No defaults,
merging, deletion, credential handling, rendering or persistence happen here.
"""

import copy
import math

from scripts.customer_migration import require


ROUTING_STRATEGIES = frozenset({
    "simple-shuffle", "least-busy", "usage-based-routing",
    "usage-based-routing-v2", "latency-based-routing", "cost-based-routing",
})
SUPPORTED_FIELDS = {
    "general_settings": frozenset({"disable_env_credential_login", "ui_access_mode"}),
    "litellm_settings": frozenset({
        "request_timeout", "drop_params", "modify_params", "force_ipv4", "set_verbose",
    }),
    "router_settings": frozenset({
        "routing_strategy", "num_retries", "allowed_fails", "cooldown_time",
        "timeout", "stream_timeout", "retry_after", "enable_pre_call_checks",
    }),
}
_BOOLEANS = frozenset({
    "disable_env_credential_login", "drop_params", "modify_params", "force_ipv4",
    "enable_pre_call_checks",
})
_INTEGERS = frozenset({"num_retries", "allowed_fails"})
_POSITIVE_NUMBERS = frozenset({"request_timeout", "timeout", "stream_timeout"})


def _validate_field(field, value):
    if field in _BOOLEANS:
        require(type(value) is bool, "Runtime setting must be a boolean literal")
    elif field == "set_verbose":
        require(value is False, "Runtime verbose logging may only be explicitly disabled")
    elif field == "ui_access_mode":
        require(type(value) is str and value in {"all", "admin_only"},
                "Runtime UI access mode must be all or admin_only")
    elif field == "routing_strategy":
        require(type(value) is str and value in ROUTING_STRATEGIES,
                "Runtime routing strategy must be a supported built-in strategy")
    elif field in _INTEGERS:
        require(type(value) is int and value >= 0,
                "Runtime retry/failure count must be a nonnegative integer literal")
    else:
        require(type(value) in {int, float},
                "Runtime duration must be a numeric literal, not a boolean, string or null")
        try:
            finite = math.isfinite(value)
        except OverflowError:
            finite = False
        require(finite, "Runtime duration must be finite")
        require(value > 0 if field in _POSITIVE_NUMBERS else value >= 0,
                "Runtime duration must be positive for timeouts and nonnegative otherwise")


def validate_runtime_settings(value):
    """Return a deep copy of valid overrides; omission preserves existing values."""
    require(isinstance(value, dict), "Runtime settings must be an object")
    require(not value.keys() - SUPPORTED_FIELDS.keys(),
            "Unsupported/reserved runtime section")
    for section, settings in value.items():
        require(isinstance(settings, dict), "Runtime sections must be objects")
        require(not settings.keys() - SUPPORTED_FIELDS[section],
                "Unsupported/reserved runtime field")
        for field, setting in settings.items():
            require(setting is not None,
                    "Runtime settings cannot be null; omit settings to preserve existing values")
            _validate_field(field, setting)
    return copy.deepcopy(value)


def parse_patch(document):
    """Validate a schema_version=1 patch and return only its setting sections."""
    require(isinstance(document, dict), "Runtime patch must be an object")
    require(not document.keys() - (SUPPORTED_FIELDS.keys() | {"schema_version"}),
            "Unsupported/reserved runtime patch field")
    require(type(document.get("schema_version")) is int and document["schema_version"] == 1,
            "Runtime patch schema_version must be the integer 1")
    settings = validate_runtime_settings({
        section: value for section, value in document.items() if section != "schema_version"
    })
    require(any(settings.values()), "Runtime patch must contain at least one setting")
    return settings


def runtime_overrides(config):
    """Validate the optional application.runtimeSettings configuration object."""
    require(isinstance(config, dict), "Runtime configuration must be an object")
    application = config.get("application", {})
    require(isinstance(application, dict), "Runtime application configuration must be an object")
    return validate_runtime_settings(application.get("runtimeSettings", {}))


def apply_runtime_settings(runtime, overrides):
    """Copy runtime and merge validated leaves without inspecting unrelated content."""
    settings = validate_runtime_settings(overrides)
    require(isinstance(runtime, dict), "Runtime configuration must be an object")
    result = copy.deepcopy(runtime)
    for section, values in settings.items():
        require(section not in result or isinstance(result[section], dict),
                "Existing targeted runtime sections must be objects")
        result.setdefault(section, {}).update(values)
    return result
