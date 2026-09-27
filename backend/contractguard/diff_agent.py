"""
Diff Agent — Mahi's core deliverable.

Usage:
    python diff_agent.py <old_openapi.json> <new_openapi.json> <output_diff_report.json>
"""

import json
import sys
from datetime import UTC, datetime
from pathlib import Path


def load_spec(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))

def _resolve_ref(obj: dict) -> str | None:
    if not isinstance(obj, dict):
        return None
    if "$ref" in obj:
        return obj["$ref"].split("/")[-1]
    if "items" in obj and isinstance(obj["items"], dict) and "$ref" in obj["items"]:
        return obj["items"]["$ref"].split("/")[-1]
    return None


def _extract_type(field_def: dict) -> str:
    if not isinstance(field_def, dict):
        return "unknown"
    if "type" in field_def:
        t = field_def["type"]
        if isinstance(t, list):
            types = [x for x in t if x != "null"]
            return types[0] if types else "null"
        return str(t)
    if "anyOf" in field_def or "oneOf" in field_def:
        variants = field_def.get("anyOf") or field_def.get("oneOf") or []
        for v in variants:
            if isinstance(v, dict):
                if "$ref" in v:
                    return v["$ref"].split("/")[-1]
                if "type" in v:
                    return str(v["type"])
    if "$ref" in field_def:
        return field_def["$ref"].split("/")[-1]
    return "unknown"


# Constraint keys compared during rename detection.  Only scalar, comparable
# keywords are included — $ref / allOf / anyOf / properties are intentionally
# excluded because they are handled structurally elsewhere.
_CONSTRAINT_KEYS = (
    "format",
    "maxLength",
    "minLength",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "pattern",
    "enum",
    "multipleOf",
    "maxItems",
    "minItems",
    "uniqueItems",
)


def _extract_constraints(field_def: dict) -> frozenset:
    """Return a frozenset of (key, value) pairs for the comparable constraints
    present in *field_def*.  Values must be hashable; lists are converted to
    tuples so they can be put in the frozenset.
    """
    if not isinstance(field_def, dict):
        return frozenset()
    pairs = []
    for k in _CONSTRAINT_KEYS:
        if k not in field_def:
            continue
        v = field_def[k]
        if isinstance(v, list):
            v = tuple(v)
        pairs.append((k, v))
    return frozenset(pairs)


def _unwrap_paginated_list(schema: dict, components: dict) -> dict | None:
    """Return the inner item schema if *schema* is a paginated-list wrapper.

    Detects the pattern::

        {
          "properties": {
            "data": {"type": "array", "items": {"$ref": "#/components/schemas/Foo"}},
            "count": {"type": "integer"},
            ...
          }
        }

    Returns the resolved ``Foo`` schema when the pattern matches, otherwise
    ``None`` so callers know no unwrapping occurred.
    """
    props = schema.get("properties", {})
    if not isinstance(props, dict):
        return None
    data_def = props.get("data")
    if not isinstance(data_def, dict):
        return None
    if data_def.get("type") != "array":
        return None
    items = data_def.get("items")
    if not isinstance(items, dict) or "$ref" not in items:
        return None
    ref_name = items["$ref"].split("/")[-1]
    return components.get(ref_name)


def _extract_fields_from_schema(
    schema: dict, components: dict
) -> dict[str, str]:
    """Return ``{field_name: type_string}`` for every property in *schema*."""
    return {
        name: type_str
        for name, (type_str, _) in _extract_fields_with_constraints(
            schema, components
        ).items()
    }


def _extract_fields_with_constraints(
    schema: dict, components: dict
) -> dict[str, tuple[str, frozenset]]:
    """Return ``{field_name: (type_string, constraints_frozenset)}``.

    The constraints frozenset contains ``(key, value)`` pairs for every
    recognised constraint keyword present on the field definition, enabling
    fine-grained comparison during rename detection.
    """
    fields: dict[str, tuple[str, frozenset]] = {}
    if not isinstance(schema, dict):
        return fields

    ref_name = _resolve_ref(schema)
    if ref_name and ref_name in components:
        schema = components[ref_name]

    # Unwrap paginated list wrapper {data: [...], count: int} → inner item schema
    inner = _unwrap_paginated_list(schema, components)
    if inner is not None:
        schema = inner

    # Unwrap allOf / anyOf wrappers
    if "allOf" in schema:
        for sub in schema["allOf"]:
            fields.update(_extract_fields_with_constraints(sub, components))

    props = schema.get("properties", {})
    if isinstance(props, dict):
        for fname, fdef in props.items():
            fields[fname] = (_extract_type(fdef), _extract_constraints(fdef))

    return fields


def _iter_endpoint_field_defs(spec: dict):
    """Yield ``(key, fields)`` for every endpoint in *spec*.

    *fields* is the result of ``_extract_fields_with_constraints`` —
    ``{field_name: (type_string, constraints_frozenset)}``.
    Used internally to build both the plain and the rich endpoint maps.
    """
    paths = spec.get("paths", {})
    components = spec.get("components", {}).get("schemas", {})

    for path, methods in paths.items():
        if not isinstance(methods, dict):
            continue
        for method, details in methods.items():
            m = method.upper()
            if m not in ("GET", "POST", "PUT", "DELETE", "PATCH"):
                continue

            key = f"{m} {path}"
            fields: dict[str, tuple[str, frozenset]] = {}

            # 1. Extract from Request Body (POST, PUT, PATCH)
            req_body = details.get("requestBody", {})
            if isinstance(req_body, dict):
                content = req_body.get("content", {}).get("application/json", {})
                req_schema = content.get("schema", {})
                fields.update(_extract_fields_with_constraints(req_schema, components))

            # 2. Extract from Responses (200, 201, 204, or default)
            responses = details.get("responses", {})
            if isinstance(responses, dict):
                res_obj = (
                    responses.get("200")
                    or responses.get("201")
                    or responses.get("204")
                    or responses.get("200 OK")
                    or {}
                )
                if isinstance(res_obj, dict):
                    content = res_obj.get("content", {}).get("application/json", {})
                    res_schema = content.get("schema", {})
                    fields.update(
                        _extract_fields_with_constraints(res_schema, components)
                    )

            yield key, fields


def get_endpoint_schemas(spec: dict) -> dict:
    result = {}
    for key, rich_fields in _iter_endpoint_field_defs(spec):
        plain = {name: type_str for name, (type_str, _) in rich_fields.items()}
        # Fallback if no explicit fields resolved
        if not plain:
            method = key.split(" ", 1)[0]
            plain["$response"] = "void" if method == "DELETE" else "unknown"
        result[key] = plain
    return result


def _get_endpoint_schemas_with_constraints(spec: dict) -> dict:
    """Like ``get_endpoint_schemas`` but values are
    ``{field_name: (type_string, constraints_frozenset)}``.
    Used by ``diff_specs`` for rename detection.
    """
    result = {}
    for key, rich_fields in _iter_endpoint_field_defs(spec):
        if not rich_fields:
            method = key.split(" ", 1)[0]
            fallback_type = "void" if method == "DELETE" else "unknown"
            rich_fields = {"$response": (fallback_type, frozenset())}
        result[key] = rich_fields
    return result


def diff_specs(old_spec: dict, new_spec: dict) -> list[dict]:
    old_endpoints = get_endpoint_schemas(old_spec)
    new_endpoints = get_endpoint_schemas(new_spec)
    # Rich maps used only for rename detection — not exposed in output fragments.
    old_rich = _get_endpoint_schemas_with_constraints(old_spec)
    new_rich = _get_endpoint_schemas_with_constraints(new_spec)
    changes: list[dict] = []
    now = datetime.now(UTC).isoformat()

    def base_entry(
        key: str,
        change_type: str,
        breaking: bool,
        old_frag: dict,
        new_frag: dict,
        severity: str,
    ) -> dict:
        method, endpoint = key.split(" ", 1)
        return {
            "endpoint": endpoint,
            "method": method,
            "change_type": change_type,
            "breaking": breaking,
            "old_schema_fragment": old_frag,
            "new_schema_fragment": new_frag,
            "severity": severity,
            "detected_at": now,
        }

    for key, old_fields in old_endpoints.items():
        if key not in new_endpoints:
            changes.append(
                base_entry(key, "endpoint_removed", True, old_fields, {}, "high")
            )
            continue

        new_fields = new_endpoints[key]
        removed = set(old_fields) - set(new_fields)
        added = set(new_fields) - set(old_fields)
        common = set(old_fields) & set(new_fields)

        # Rich constraint maps for this endpoint (used only in rename detection).
        old_rich_fields = old_rich.get(key, {})
        new_rich_fields = new_rich.get(key, {})

        # Rename heuristic: a removed field is only classified as renamed when
        # an added field shares *both* the same base type *and* the same
        # constraint fingerprint (format, maxLength, minLength, …).
        # Fields that merely share a type but differ in constraints fall through
        # to separate field_removed + field_added_required entries.
        for r in list(removed):
            old_type, old_constraints = old_rich_fields.get(r, (old_fields[r], frozenset()))
            match = next(
                (
                    a
                    for a in added
                    if new_rich_fields.get(a, (new_fields[a], frozenset()))
                    == (old_type, old_constraints)
                ),
                None,
            )
            if match:
                changes.append(
                    base_entry(
                        key,
                        "field_renamed",
                        True,
                        {r: old_fields[r]},
                        {match: new_fields[match]},
                        "high",
                    )
                )
                removed.discard(r)
                added.discard(match)

        for r in removed:
            changes.append(
                base_entry(key, "field_removed", True, {r: old_fields[r]}, {}, "medium")
            )

        for a in added:
            changes.append(
                base_entry(
                    key, "field_added_required", False, {}, {a: new_fields[a]}, "low"
                )
            )

        for f in common:
            if old_fields[f] != new_fields[f]:
                changes.append(
                    base_entry(
                        key,
                        "field_type_changed",
                        True,
                        {f: old_fields[f]},
                        {f: new_fields[f]},
                        "high",
                    )
                )

    return changes


def main():
    if len(sys.argv) != 4:
        print(
            "Usage: python diff_agent.py <old_openapi.json> <new_openapi.json> <output.json>"
        )
        sys.exit(1)

    old_path, new_path, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
    old_spec = load_spec(old_path)
    new_spec = load_spec(new_path)

    changes = diff_specs(old_spec, new_spec)

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_text(json.dumps(changes, indent=2))
    print(f"Wrote {len(changes)} change(s) to {out_path}")
    for c in changes:
        print(
            f"  - [{c['severity']}] {c['change_type']} on {c['method']} {c['endpoint']}"
        )


if __name__ == "__main__":
    main()
