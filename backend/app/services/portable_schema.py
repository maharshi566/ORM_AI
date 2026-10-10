"""Tool parameter schemas that every OpenAI-compatible provider accepts.

Pydantic writes full JSON Schema: ``$ref``/``$defs`` for nested models, ``anyOf`` with
``null`` for optional fields, ``additionalProperties``, ``title``, ``default``,
``format: date``, ``exclusiveMinimum``. OpenAI accepts all of it, but other providers
reached through their OpenAI-style endpoint do not: Google Gemini rejects a request
whose tool parameters contain ``additionalProperties`` or ``$ref``, for example.

``portable_parameters`` rewrites a schema into the small common subset (type,
description, properties, required, items, enum, and the numeric, length and
array-size limits), and moves what it drops into the description where a model can
still read it ("Default: 20.", "Date as YYYY-MM-DD."). Nothing is lost for safety:
the ToolRegistry still validates every call against the tool's full Pydantic model
and rejects unknown fields, so a looser schema can never let a bad call through.
"""

import json
from typing import Any

KEPT = (
    "type",
    "description",
    "enum",
    "minimum",
    "maximum",
    "minLength",
    "maxLength",
    "minItems",
    "maxItems",
)

FORMAT_HINTS = {
    "date": "Date as YYYY-MM-DD.",
    "date-time": "Date and time in ISO 8601, for example 2026-10-09T14:30:00+05:30.",
    "time": "Time as HH:MM:SS.",
    "email": "An email address.",
    "uri": "A URL.",
}

MAX_DEPTH = 12  # nested models deeper than this are cut off (a $ref loop, in practice)


def _resolve(node: dict[str, Any], defs: dict[str, Any]) -> dict[str, Any]:
    reference = node.get("$ref")
    if not isinstance(reference, str) or not reference.startswith("#/$defs/"):
        return node
    target = defs.get(reference.removeprefix("#/$defs/"), {})
    merged = {**target, **{k: v for k, v in node.items() if k != "$ref"}}
    return merged


def _without_null(options: list[Any]) -> list[dict[str, Any]]:
    return [o for o in options if isinstance(o, dict) and o.get("type") != "null"]


def _hint(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value


def _convert(node: Any, defs: dict[str, Any], depth: int) -> dict[str, Any]:
    if not isinstance(node, dict):
        return {}
    node = _resolve(node, defs)
    notes: list[str] = []

    # Optional fields: anyOf [X, null] becomes X. A choice of several real types
    # (Decimal is "number or numeric string") keeps the first one.
    for key in ("anyOf", "oneOf"):
        if key in node:
            options = _without_null(node[key])
            rest = {k: v for k, v in node.items() if k != key}
            if options:
                chosen = _resolve(options[0], defs)
                node = {**chosen, **rest}
            else:
                node = rest
    if "allOf" in node and isinstance(node["allOf"], list):
        rest = {k: v for k, v in node.items() if k != "allOf"}
        for part in node["allOf"]:
            if isinstance(part, dict):
                rest = {**_resolve(part, defs), **rest}
        node = rest

    out: dict[str, Any] = {key: node[key] for key in KEPT if key in node}
    if "const" in node and "enum" not in out:
        out["enum"] = [node["const"]]
    if isinstance(out.get("type"), list):  # ["string", "null"] -> "string"
        types = [t for t in out["type"] if t != "null"]
        out["type"] = types[0] if types else "string"
    if "type" not in out:
        if "properties" in node:
            out["type"] = "object"
        elif "items" in node:
            out["type"] = "array"
        elif "enum" in out and out["enum"]:
            out["type"] = "integer" if isinstance(out["enum"][0], int) else "string"

    if "exclusiveMinimum" in node:
        notes.append(f"Must be greater than {_hint(node['exclusiveMinimum'])}.")
    if "exclusiveMaximum" in node:
        notes.append(f"Must be less than {_hint(node['exclusiveMaximum'])}.")
    if isinstance(node.get("format"), str) and node["format"] in FORMAT_HINTS:
        notes.append(FORMAT_HINTS[node["format"]])
    if node.get("default") is not None:
        notes.append(f"Default: {_hint(node['default'])}.")

    if out.get("type") == "object" or "properties" in node:
        properties = node.get("properties") or {}
        if depth < MAX_DEPTH:
            out["properties"] = {
                name: _convert(sub, defs, depth + 1) for name, sub in properties.items()
            }
        else:
            out["properties"] = {}
        required = [name for name in node.get("required", []) if name in out["properties"]]
        if required:
            out["required"] = required
    if "items" in node and depth < MAX_DEPTH:
        out["items"] = _convert(node["items"], defs, depth + 1)
    elif out.get("type") == "array":
        out["items"] = {"type": "string"}

    if notes:
        description = out.get("description", "").strip()
        out["description"] = " ".join([description, *notes] if description else notes)
    return out


def portable_parameters(schema: dict[str, Any]) -> dict[str, Any]:
    """The tool's parameter schema in the subset every provider accepts."""
    defs = {**schema.get("definitions", {}), **schema.get("$defs", {})}
    converted = _convert(schema, defs, 0)
    converted["type"] = "object"
    converted.setdefault("properties", {})
    return converted


def portable_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Function definitions with portable parameter schemas (other fields unchanged)."""
    result = []
    for tool in tools:
        function = tool.get("function")
        if tool.get("type") != "function" or not isinstance(function, dict):
            result.append(tool)
            continue
        parameters = function.get("parameters")
        portable = {**function}
        if isinstance(parameters, dict):
            portable["parameters"] = portable_parameters(parameters)
        result.append({**tool, "function": portable})
    return result
