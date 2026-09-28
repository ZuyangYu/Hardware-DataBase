"""Small, bounded JSON-schema subset used at sandbox trust boundaries.

The HDB control plane must not treat a schema received from a Runner or MCP
server as an authorization policy.  Catalogs therefore store a reviewed,
bounded subset and validate values locally before forwarding them.  This is
intentionally not a general JSON-Schema implementation: unsupported features
are rejected rather than interpreted permissively.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any


class SchemaValidationError(ValueError):
    """A reviewed schema or a value does not satisfy the bounded contract."""


_SCHEMA_TYPES = frozenset({"object", "array", "string", "number", "integer", "boolean", "null"})
_ALLOWED_SCHEMA_KEYS = frozenset(
    {
        "type",
        "title",
        "description",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "enum",
        "minLength",
        "maxLength",
        "minimum",
        "maximum",
        "minItems",
        "maxItems",
    }
)
_MAX_SCHEMA_BYTES = 64 * 1024
_MAX_SCHEMA_DEPTH = 12
_MAX_PROPERTIES = 128
_MAX_ENUM_VALUES = 128


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _bounded_string(value: Any, field: str, limit: int = 2000) -> str:
    if not isinstance(value, str) or len(value) > limit or any(ord(char) < 32 for char in value):
        raise SchemaValidationError(f"{field} must be bounded text")
    return value


def _normalize_node(value: Any, *, path: str, depth: int) -> dict[str, Any]:
    if depth > _MAX_SCHEMA_DEPTH:
        raise SchemaValidationError(f"{path} is too deeply nested")
    if not isinstance(value, Mapping):
        raise SchemaValidationError(f"{path} must be an object")
    unknown = set(str(key) for key in value) - _ALLOWED_SCHEMA_KEYS
    if unknown:
        raise SchemaValidationError(f"{path} contains unsupported schema keys: {', '.join(sorted(unknown))}")

    result: dict[str, Any] = {}
    raw_type = value.get("type")
    if raw_type is not None:
        if isinstance(raw_type, (list, tuple)):
            types = [str(item) for item in raw_type]
            if not types or any(item not in _SCHEMA_TYPES for item in types):
                raise SchemaValidationError(f"{path}.type is invalid")
            result["type"] = sorted(set(types))
        elif str(raw_type) not in _SCHEMA_TYPES:
            raise SchemaValidationError(f"{path}.type is invalid")
        else:
            result["type"] = str(raw_type)

    for key in ("title", "description"):
        if key in value:
            result[key] = _bounded_string(value[key], f"{path}.{key}")

    if "enum" in value:
        enum = value["enum"]
        if not isinstance(enum, (list, tuple)) or len(enum) > _MAX_ENUM_VALUES:
            raise SchemaValidationError(f"{path}.enum is invalid")
        try:
            encoded = _canonical(list(enum)).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise SchemaValidationError(f"{path}.enum must be JSON-compatible") from exc
        if len(encoded) > 16 * 1024:
            raise SchemaValidationError(f"{path}.enum is too large")
        result["enum"] = list(enum)

    if "properties" in value:
        properties = value["properties"]
        if not isinstance(properties, Mapping) or len(properties) > _MAX_PROPERTIES:
            raise SchemaValidationError(f"{path}.properties is invalid")
        normalized_properties: dict[str, Any] = {}
        for raw_name, child in properties.items():
            name = str(raw_name)
            if not name or len(name) > 120 or any(ord(char) < 32 for char in name):
                raise SchemaValidationError(f"{path}.properties contains an invalid name")
            normalized_properties[name] = _normalize_node(child, path=f"{path}.properties.{name}", depth=depth + 1)
        result["properties"] = normalized_properties

    if "required" in value:
        required = value["required"]
        if not isinstance(required, (list, tuple)) or len(required) > _MAX_PROPERTIES:
            raise SchemaValidationError(f"{path}.required is invalid")
        names = [str(item) for item in required]
        if any(not item or len(item) > 120 for item in names):
            raise SchemaValidationError(f"{path}.required contains an invalid name")
        properties = set(result.get("properties", {}))
        if properties and not set(names).issubset(properties):
            raise SchemaValidationError(f"{path}.required references an unknown property")
        result["required"] = sorted(set(names))

    if "additionalProperties" in value:
        additional = value["additionalProperties"]
        if not isinstance(additional, bool):
            # A schema-valued additionalProperties rule would make the policy
            # surface larger; keep the first implementation fail-closed.
            raise SchemaValidationError(f"{path}.additionalProperties must be boolean")
        result["additionalProperties"] = additional

    if "items" in value:
        result["items"] = _normalize_node(value["items"], path=f"{path}.items", depth=depth + 1)

    integer_limits = {
        "minLength": (0, 1_000_000),
        "maxLength": (0, 1_000_000),
        "minItems": (0, 100_000),
        "maxItems": (0, 100_000),
    }
    for key, (lower, upper) in integer_limits.items():
        if key in value:
            try:
                number = int(value[key])
            except (TypeError, ValueError) as exc:
                raise SchemaValidationError(f"{path}.{key} must be an integer") from exc
            if number < lower or number > upper:
                raise SchemaValidationError(f"{path}.{key} is out of bounds")
            result[key] = number
    for key in ("minimum", "maximum"):
        if key in value:
            try:
                number = float(value[key])
            except (TypeError, ValueError) as exc:
                raise SchemaValidationError(f"{path}.{key} must be numeric") from exc
            if number != number or number in {float("inf"), float("-inf")}:
                raise SchemaValidationError(f"{path}.{key} is invalid")
            result[key] = number
    if "minLength" in result and "maxLength" in result and result["minLength"] > result["maxLength"]:
        raise SchemaValidationError(f"{path} has an invalid length range")
    if "minItems" in result and "maxItems" in result and result["minItems"] > result["maxItems"]:
        raise SchemaValidationError(f"{path} has an invalid item range")
    if "minimum" in result and "maximum" in result and result["minimum"] > result["maximum"]:
        raise SchemaValidationError(f"{path} has an invalid numeric range")
    return result


def normalize_schema(value: Any, *, field_name: str = "schema") -> dict[str, Any]:
    """Validate and canonicalize a reviewed schema declaration.

    An empty mapping means that the catalog intentionally imposes no additional
    value shape.  It does *not* grant runtime authority; the surrounding bundle
    or Gateway allowlist still controls whether a value can be sent at all.
    """

    if value in (None, ""):
        return {}
    if not isinstance(value, Mapping):
        raise SchemaValidationError(f"{field_name} must be an object")
    normalized = _normalize_node(value, path=field_name, depth=0)
    if len(_canonical(normalized).encode("utf-8")) > _MAX_SCHEMA_BYTES:
        raise SchemaValidationError(f"{field_name} exceeds the size limit")
    return normalized


def schema_digest(value: Mapping[str, Any] | None) -> str:
    import hashlib

    normalized = normalize_schema(value or {})
    return hashlib.sha256(_canonical(normalized).encode("utf-8")).hexdigest()


def _type_matches(value: Any, expected: str) -> bool:
    if expected == "null":
        return value is None
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "string":
        return isinstance(value, str)
    if expected == "array":
        return isinstance(value, (list, tuple))
    if expected == "object":
        return isinstance(value, Mapping)
    return False


def validate_value(value: Any, schema: Mapping[str, Any] | None, *, path: str = "value") -> None:
    """Validate one JSON-compatible value against a normalized schema."""

    normalized = normalize_schema(schema or {})
    if not normalized:
        return
    expected = normalized.get("type")
    if expected is not None:
        types = expected if isinstance(expected, list) else [expected]
        if not any(_type_matches(value, str(item)) for item in types):
            raise SchemaValidationError(f"{path} does not match the reviewed type")
    if "enum" in normalized and value not in normalized["enum"]:
        raise SchemaValidationError(f"{path} is not an allowed value")
    if isinstance(value, str):
        if "minLength" in normalized and len(value) < normalized["minLength"]:
            raise SchemaValidationError(f"{path} is shorter than allowed")
        if "maxLength" in normalized and len(value) > normalized["maxLength"]:
            raise SchemaValidationError(f"{path} is longer than allowed")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in normalized and value < normalized["minimum"]:
            raise SchemaValidationError(f"{path} is below the minimum")
        if "maximum" in normalized and value > normalized["maximum"]:
            raise SchemaValidationError(f"{path} is above the maximum")
    if isinstance(value, (list, tuple)):
        if "minItems" in normalized and len(value) < normalized["minItems"]:
            raise SchemaValidationError(f"{path} has too few items")
        if "maxItems" in normalized and len(value) > normalized["maxItems"]:
            raise SchemaValidationError(f"{path} has too many items")
        item_schema = normalized.get("items")
        if item_schema is not None:
            for index, item in enumerate(value):
                validate_value(item, item_schema, path=f"{path}[{index}]")
    if isinstance(value, Mapping):
        properties = normalized.get("properties", {})
        required = set(normalized.get("required", ()))
        missing = sorted(name for name in required if name not in value)
        if missing:
            raise SchemaValidationError(f"{path} is missing required fields: {', '.join(missing)}")
        if normalized.get("additionalProperties") is False:
            unknown = set(str(key) for key in value) - set(properties)
            if unknown:
                raise SchemaValidationError(f"{path} contains unknown fields: {', '.join(sorted(unknown))}")
        for name, child_schema in properties.items():
            if name in value:
                validate_value(value[name], child_schema, path=f"{path}.{name}")


__all__ = ["SchemaValidationError", "normalize_schema", "schema_digest", "validate_value"]
