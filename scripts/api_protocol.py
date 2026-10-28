"""Reviewed API method gate: inference POST, model-list GET and Responses WebSocket handshakes; never an Allow rule."""

# Codex model discovery: GET only, never /v1/models/{id} or other management routes.
MODEL_LIST_PATHS = ("/models", "/v1/models")
MODEL_LIST_RATE_RULE = "RateLimitModelList"
# Per client IP per minute: the legacy ingress allowed 2 requests/second on /v1/models.
MODEL_LIST_RATE_LIMIT = 120
# Front Door matches RequestUri on the full URL; Application Gateway on the path and query only.
_ORIGIN = "(https://[^/?#]+)?"
RESPONSES_URI = rf"^{_ORIGIN}/(v1/)?responses(\?.*)?$"
MODEL_LIST_URI = rf"^{_ORIGIN}/(v1/)?models(\?.*)?$"
API_GET_URI = rf"^{_ORIGIN}/(v1/)?(responses|models)(\?.*)?$"
RATE_DURATIONS = {1: "OneMin", 5: "FiveMins"}


def _condition(variable, operator, values, negate=False, **extra):
    return {"matchVariable": variable, "operator": operator, **({"negateCondition": True} if negate else {}), "matchValue": list(values), **extra}


def api_method_rules(model_list=True):
    """Front Door API match rules; model_list=False is the earlier Responses-only baseline that api_wss upgrades."""
    get = lambda: _condition("RequestMethod", "Equal", ["GET"])
    # The WebSocket header checks apply to Responses GETs only; a model-list GET is plain HTTP.
    responses_get = lambda: [get(), _condition("RequestUri", "RegEx", [RESPONSES_URI])] if model_list else [get()]
    specifications = [
        ("BlockUnsupportedApiMethods", 10, [_condition("RequestMethod", "Equal", ["POST", "GET"], True)]),
        # Name kept for WAF log continuity; it now also admits the model-list GET.
        ("BlockNonResponsesGet", 11, [get(), _condition("RequestUri", "RegEx", [API_GET_URI if model_list else RESPONSES_URI], True)]),
        ("BlockInvalidWebSocketUpgrade", 12, [*responses_get(), _condition("RequestHeader", "Equal", ["websocket"], True, selector="Upgrade", transforms=["Lowercase"])]),
        ("BlockInvalidWebSocketConnection", 13, [*responses_get(), _condition("RequestHeader", "RegEx", [r"(^|[, \t])upgrade([, \t]|$)"], True, selector="Connection", transforms=["Lowercase"])]),
    ]
    if model_list:
        specifications.append(("BlockModelListNonGet", 14, [_condition("RequestMethod", "Equal", ["GET"], True), _condition("RequestUri", "RegEx", [MODEL_LIST_URI])]))
    return [
        {"name": name, "priority": priority, "enabledState": "Enabled",
         "ruleType": "MatchRule", "action": "Block", "matchConditions": conditions}
        for name, priority, conditions in specifications
    ]


def model_list_rate_rule(threshold):
    """Per-client-IP model-discovery limit, evaluated before the global RateLimitApi (priority 20)."""
    return {"name": MODEL_LIST_RATE_RULE, "priority": 15, "enabledState": "Enabled", "ruleType": "RateLimitRule",
            "rateLimitDurationInMinutes": 1, "rateLimitThreshold": threshold, "groupBy": [{"variableName": "SocketAddr"}],
            "action": "Block", "matchConditions": [_condition("RequestUri", "RegEx", [MODEL_LIST_URI])]}


def app_gateway_rule(rule):
    """One Front Door API rule in the Application Gateway WAF custom-rule schema."""
    variables = {"RequestMethod": "RequestMethod", "RequestUri": "RequestUri", "RequestHeader": "RequestHeaders"}
    operators = {"Equal": "Equal", "RegEx": "Regex"}
    conditions = []
    for condition in rule["matchConditions"]:
        variable = {"variableName": variables[condition["matchVariable"]]}
        if "selector" in condition:
            variable["selector"] = condition["selector"]
        values = condition["matchValue"]
        if condition["matchVariable"] == "RequestUri":
            # Application Gateway matches RequestUri on the path and query only, never a scheme or host.
            values = [value.replace(_ORIGIN, "") for value in values]
        converted = {"matchVariables": [variable], "operator": operators[condition["operator"]],
                     "negationConditon": condition.get("negateCondition", False), "matchValues": values}
        if "transforms" in condition:
            converted["transforms"] = condition["transforms"]
        conditions.append(converted)
    result = {"name": rule["name"], "priority": rule["priority"], "state": "Enabled", "ruleType": rule["ruleType"], "action": "Block"}
    if rule["ruleType"] == "RateLimitRule":
        result.update(rateLimitDuration=RATE_DURATIONS[rule["rateLimitDurationInMinutes"]], rateLimitThreshold=rule["rateLimitThreshold"],
                      groupByUserSession=[{"groupByVariables": [{"variableName": "ClientAddr"}]}])
    result["matchConditions"] = conditions
    return result


def app_gateway_api_method_rules():
    """The same method gate in the Application Gateway WAF custom-rule schema."""
    return [app_gateway_rule(rule) for rule in api_method_rules()]


def app_gateway_model_list_rate_rule(threshold):
    return app_gateway_rule(model_list_rate_rule(threshold))
