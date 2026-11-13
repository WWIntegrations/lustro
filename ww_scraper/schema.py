"""Output, dataset and key-value store schemas declared by an actor.

    output_schema  {name: {"title", "template"}}   resolved into the run record as `output`
                   templates use {id}, {defaultDatasetId}, {defaultKeyValueStoreId},
                   {defaultRequestQueueId}; the result is a path on the local API
    dataset_schema {"fields": <JSON Schema for one item>, "views": {name: {title, fields,
                   omit, flatten, unwind, desc}}}   every pushed item is checked; one invalid
                   item rejects the whole push. Views name ready-made export options.
    kv_schema      {"collections": {name: {"key" | "keyPrefix", "contentTypes": [...],
                   "jsonSchema": {...}}}}   records written to a matching key are checked

The JSON Schema subset: type (one or a list, including "null"), properties, required,
additionalProperties false, items, enum, minimum, maximum, minLength, maxLength, pattern.
"""
from __future__ import annotations

import re

_JS_TYPES = {
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "array": lambda v: isinstance(v, list),
    "object": lambda v: isinstance(v, dict),
    "null": lambda v: v is None,
}
MAX_ERRORS = 20


class SchemaValidationError(ValueError):
    def __init__(self, errors):
        self.errors = list(errors)[:MAX_ERRORS]
        super().__init__("schema validation failed: " + "; ".join(self.errors))


def check(schema: dict, value, path: str = "item") -> list:
    """Errors of value against schema (empty list when it conforms)."""
    errors: list = []
    _check(schema or {}, value, path, errors)
    return errors


def _check(schema, value, path, errors):
    if len(errors) >= MAX_ERRORS:
        return
    t = schema.get("type")
    if t:
        types = t if isinstance(t, list) else [t]
        if not any(_JS_TYPES.get(x, lambda v: False)(value) for x in types):
            errors.append(f"{path}: expected {' or '.join(types)}")
            return
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: must be one of {', '.join(map(str, schema['enum']))}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: must be >= {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: must be <= {schema['maximum']}")
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append(f"{path}: shorter than {schema['minLength']}")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(f"{path}: longer than {schema['maxLength']}")
        if schema.get("pattern"):
            try:
                if re.search(schema["pattern"], value) is None:
                    errors.append(f"{path}: does not match {schema['pattern']}")
            except re.error:
                errors.append(f"{path}: invalid pattern in schema")
    if isinstance(value, dict):
        props = schema.get("properties") or {}
        for key in schema.get("required") or []:
            if key not in value:
                errors.append(f"{path}.{key}: required")
        for key, v in value.items():
            if key in props:
                _check(props[key], v, f"{path}.{key}", errors)
            elif schema.get("additionalProperties") is False:
                errors.append(f"{path}.{key}: not allowed")
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for i, v in enumerate(value):
            _check(schema["items"], v, f"{path}[{i}]", errors)


def resolve_output(output_schema: dict, record: dict) -> dict:
    """{name: {"title", "url"}} with the record's ids filled into each template."""
    vals = {k: record.get(k) or "" for k in ("id", "defaultDatasetId", "defaultKeyValueStoreId",
                                            "defaultRequestQueueId", "actId")}
    out = {}
    for name, spec in (output_schema or {}).items():
        tpl = spec.get("template", "") if isinstance(spec, dict) else str(spec)
        url = re.sub(r"\{(\w+)\}", lambda m: str(vals.get(m.group(1), m.group(0))), tpl)
        out[name] = {"title": spec.get("title", name) if isinstance(spec, dict) else name, "url": url}
    return out


def collection_for(kv_schema: dict, key: str):
    """(name, spec) of the collection a key belongs to, or (None, None)."""
    for name, spec in ((kv_schema or {}).get("collections") or {}).items():
        if spec.get("key") == key or (spec.get("keyPrefix") and key.startswith(spec["keyPrefix"])):
            return name, spec
    return None, None


def check_record(kv_schema: dict, key: str, value, content_type: str) -> list:
    name, spec = collection_for(kv_schema, key)
    if spec is None:
        return []
    errors = []
    allowed = spec.get("contentTypes")
    base = (content_type or "").split(";")[0].strip().lower()
    if allowed and not any(base == a.lower() or (a.endswith("/*") and base.startswith(a[:-1].lower()))
                           for a in allowed):
        errors.append(f"{key}: content type {base or 'none'} not allowed in collection {name} "
                      f"({', '.join(allowed)})")
    if spec.get("jsonSchema") and base == "application/json":
        errors += check(spec["jsonSchema"], value, key)
    return errors


STR_OR_NULL = {"type": ["string", "null"]}
