"""Minimal JSON Schema validator — stdlib only.

The repo takes no runtime dependency beyond ``packaging``/``tomli`` (see
pyproject.toml), and PLAN.md §12 explicitly wants the provider layer kept to
one small, un-abstracted module rather than growing a dependency footprint
for it. This covers exactly the subset v1's own schemas use: ``type``,
``enum``, ``required``, ``properties``/``additionalProperties``, ``items``,
``minItems``, ``minimum``/``maximum``, ``minLength``/``maxLength``. It is
not a general JSON Schema implementation — reach for a real library if v2+
needs `$ref`, `oneOf`, or similar.
"""

from __future__ import annotations

import json
from typing import Any

_TYPE_MAP: dict[str, Any] = {
    "string": str,
    "number": (int, float),
    "integer": int,
    "boolean": bool,
    "array": list,
    "object": dict,
}


def validate(instance: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    """Return a list of human-readable validation errors (empty = valid)."""
    errors: list[str] = []

    expected_type = schema.get("type")
    if expected_type is not None:
        if expected_type in ("integer", "number") and isinstance(instance, bool):
            # bool is a subclass of int in Python; JSON schema treats it as
            # its own type and never as a number.
            errors.append(f"{path}: expected {expected_type}, got bool")
            return errors
        py_type = _TYPE_MAP.get(expected_type)
        if py_type is not None and not isinstance(instance, py_type):
            errors.append(f"{path}: expected {expected_type}, got {type(instance).__name__}")
            return errors

    enum = schema.get("enum")
    if enum is not None and instance not in enum:
        errors.append(f"{path}: {instance!r} not in {enum!r}")

    if isinstance(instance, str):
        if "minLength" in schema and len(instance) < schema["minLength"]:
            errors.append(f"{path}: shorter than minLength {schema['minLength']}")
        if "maxLength" in schema and len(instance) > schema["maxLength"]:
            errors.append(f"{path}: longer than maxLength {schema['maxLength']}")

    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            errors.append(f"{path}: below minimum {schema['minimum']}")
        if "maximum" in schema and instance > schema["maximum"]:
            errors.append(f"{path}: above maximum {schema['maximum']}")

    if isinstance(instance, dict):
        for key in schema.get("required", []):
            if key not in instance:
                errors.append(f"{path}.{key}: missing required property")
        properties = schema.get("properties", {})
        additional_allowed = schema.get("additionalProperties", True)
        for key, value in instance.items():
            if key in properties:
                errors.extend(validate(value, properties[key], f"{path}.{key}"))
            elif not additional_allowed:
                errors.append(f"{path}.{key}: unexpected property")

    if isinstance(instance, list):
        item_schema = schema.get("items")
        if item_schema is not None:
            for index, item in enumerate(instance):
                errors.extend(validate(item, item_schema, f"{path}[{index}]"))
        if "minItems" in schema and len(instance) < schema["minItems"]:
            errors.append(f"{path}: fewer than minItems {schema['minItems']}")

    return errors


def describe_schema(schema: dict[str, Any]) -> str:
    """Render a schema for inclusion in a prompt (PLAN.md §12 fallback path)."""
    return json.dumps(schema, indent=2, sort_keys=True)
