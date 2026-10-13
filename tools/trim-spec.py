#!/usr/bin/env python3
"""Build the trimmed OpenAPI document this client is generated from.

The panel publishes one large OpenAPI document (~160 paths). Generating a client
from all of it with ogen fails: every operation in 3.4.x declares its error
responses as an untyped `oneOf`, and ogen cannot discriminate a union that
carries no discriminator, so it skips the operation. Since the vendor requires
consumers to pin the SDK to the panel version, and the community SDK lags a
release behind, we generate our own client from the pinned specification
instead — for the operations rwx-store actually calls.

Two things are dropped on purpose, and both are recorded in the README:

  * every path outside OPERATIONS, plus the schemas unreachable from them;
  * responses whose body is a top-level `oneOf`/`anyOf` — those are error
    bodies. Consumers decode them against the real specification themselves,
    which is what rwx-store's panel adapter already does.

Any wanted path missing from the specification is a hard error: the panel
renamed or dropped something and the trimmed document must not quietly shrink.
"""

import json
import pathlib
import sys

# Paths rwx-store calls, grouped by what the adapter does with them. Keep the
# trailing comment honest — it is the reason the operation is in this list.
# The client exposes exactly these calls and nothing else: a path item may
# declare several operations and rwx-store is not obliged to inherit all of them.
OPERATIONS = {
    # user lifecycle
    "/api/users": {"post", "patch"},  # grant access, change it
    "/api/users/{userId}": {"get"},  # read state
    "/api/users/{userId}/actions/enable": {"post"},
    "/api/users/{userId}/actions/disable": {"post"},
    "/api/users/{userId}/actions/revoke": {"post"},  # rotate the link
    "/api/users/by-username/{username}": {"get"},  # idempotent creation
    # device hygiene
    "/api/hwid/devices/{userId}": {"get"},
    "/api/hwid/devices/delete": {"post"},  # unbind one device
    # squad catalogue
    "/api/internal-squads": {"get"},
}

ROOT = pathlib.Path(__file__).resolve().parent.parent

# Parameters whose declared type the vendor got wrong. The 3.4.5 specification
# types these identifiers as `number` with no integer format, and ogen renders a
# number as a fixed-point float: GET /api/users/12345 is sent as
# /api/users/12345.0000000000, which the panel rejects. The 3.4.4 specification
# said `integer`, so this is a vendor regression — and ogen's own `infer_types`
# does not rescue it (checked: with the flag the parameter still comes out as a
# float). Correcting it here is the point of owning the client.
INTEGER_PARAMS = {
    "userId": {"type": "integer", "format": "int64"},
}


def apply_integer_overrides(methods):
    """Rewrite the schema of any parameter listed in INTEGER_PARAMS."""
    corrected = 0
    holders = [methods] + [
        operation for operation in methods.values() if isinstance(operation, dict)
    ]
    for holder in holders:
        for parameter in holder.get("parameters") or []:
            if parameter.get("name") in INTEGER_PARAMS:
                parameter["schema"] = dict(INTEGER_PARAMS[parameter["name"]])
                corrected += 1
    return corrected


def collect_refs(node, into):
    """Add every `#/components/schemas/...` reference found in node to into."""
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and "/schemas/" in ref:
            into.add(ref.split("/")[-1])
        for value in node.values():
            collect_refs(value, into)
    elif isinstance(node, list):
        for item in node:
            collect_refs(item, into)


def merge_union(members, schemas):
    """Flatten a union of named object schemas into one object schema.

    The panel answers business failures and validation failures with different
    shapes, and the specification offers them as an untyped `oneOf`, which ogen
    cannot discriminate. A JSON reader does not care: it reads whichever fields
    the answer actually carries. So the union becomes a single object holding
    the union of its members' properties, with every constraint kept only where
    all members agree.

    That last part is not tidiness. The business variant pins `message` to an
    enumerated list of strings, the validation variant does not, and ogen
    validates enums while decoding — merging naively makes every message outside
    that list a decode error, and a routine 400 stops explaining itself. Every
    property and constraint here still comes from the vendor's own schemas;
    nothing is invented.
    """
    properties, declared = {}, {}
    required = None
    for name in members:
        member = schemas.get(name) or {}
        own_properties = member.get("properties") or {}
        for key, value in own_properties.items():
            declared.setdefault(key, []).append(value)
            properties.setdefault(key, value)
        own = member.get("required")
        required = (
            set(own)
            if required is None and own
            else (set(own) & required if own and required is not None else set())
        )

    for key, seen in declared.items():
        merged_property = dict(properties[key])
        for constraint in ("enum", "format"):
            values = {json.dumps(item.get(constraint), sort_keys=True) for item in seen}
            # A constraint survives only if every member declares this property
            # and all of them declare the same constraint.
            if len(seen) != len(members) or len(values) > 1:
                merged_property.pop(constraint, None)
        properties[key] = merged_property

    merged = {"type": "object", "properties": properties}
    if required:
        merged["required"] = sorted(required)
    return merged


def flatten_union_responses(methods, path, schemas, flattened, created):
    """Replace every union response body with a reference to a merged schema."""
    for method, operation in methods.items():
        if not isinstance(operation, dict):
            continue
        for status, response in (operation.get("responses") or {}).items():
            for media in (response.get("content") or {}).values():
                schema = media.get("schema") or {}
                union = schema.get("oneOf") or schema.get("anyOf")
                if not union:
                    continue
                members = tuple(
                    sorted(
                        item["$ref"].split("/")[-1]
                        for item in union
                        if isinstance(item, dict) and "$ref" in item
                    )
                )
                if not members or len(members) != len(union):
                    raise SystemExit(
                        f"{method.upper()} {path} -> {status}: cannot flatten a union "
                        "whose members are inline schemas"
                    )
                name = "RwxUnified" + members[0]
                if name not in schemas:
                    schemas[name] = merge_union(members, schemas)
                    created.append(name)
                flattened.append(f"{method.upper():6} {path} -> {status}")
                media["schema"] = {"$ref": f"#/components/schemas/{name}"}


def main(source, target):
    doc = json.loads(pathlib.Path(source).read_text())
    schemas = (doc.get("components") or {}).get("schemas") or {}
    paths = doc.get("paths") or {}

    absent = []
    for path, wanted in OPERATIONS.items():
        present = set(paths.get(path) or {})
        for method in sorted(wanted - present):
            absent.append(f"{method.upper()} {path}")
    if absent:
        sys.exit(
            "These operations are absent from "
            + str(source)
            + ":\n  "
            + "\n  ".join(absent)
            + "\nThe panel changed its API. Update OPERATIONS before regenerating."
        )

    keep = set()
    kept_paths = {}
    flattened = []
    created = []
    corrected = 0
    for path, wanted in OPERATIONS.items():
        item = paths[path]
        methods = {m: v for m, v in item.items() if m in wanted}
        # Parameters may live on the path item itself and apply to every
        # operation; keeping them is what makes the path template resolvable.
        if "parameters" in item:
            methods["parameters"] = item["parameters"]
        kept_paths[path] = methods
        flatten_union_responses(methods, path, schemas, flattened, created)
        corrected += apply_integer_overrides(methods)
        collect_refs(methods, keep)

    # Transitive closure: a kept schema references more schemas.
    while True:
        before = len(keep)
        for name in list(keep):
            if name in schemas:
                collect_refs(schemas[name], keep)
        if len(keep) == before:
            break

    components = {k: v for k, v in (doc.get("components") or {}).items() if k != "schemas"}
    components["schemas"] = {n: schemas[n] for n in sorted(keep) if n in schemas}

    info = doc.get("info") or {}
    out = {
        "openapi": doc.get("openapi", "3.0.0"),
        "info": {
            "title": info.get("title", "Remnawave API"),
            "version": info.get("version", "unknown"),
            "description": (
                "Trimmed specification: only the operations rwx-store calls. "
                "Union error bodies are omitted; see tools/trim-spec.py."
            ),
        },
        "servers": doc.get("servers", []),
        "paths": kept_paths,
        "components": components,
    }
    pathlib.Path(target).write_text(json.dumps(out, ensure_ascii=False, indent=1))

    operations = sum(
        1
        for methods in kept_paths.values()
        for method in methods
        if method in ("get", "post", "put", "patch", "delete")
    )
    print(f"paths {len(paths)} -> {len(kept_paths)} ({operations} operations)")
    print(f"schemas {len(schemas)} -> {len(components['schemas'])}")
    print(f"union responses flattened: {len(flattened)}, schemas created: {len(created)}")
    print(f"integer param corrections: {corrected}")
    for line in flattened:
        print(f"  {line}")


if __name__ == "__main__":
    args = sys.argv[1:]
    if len(args) != 2:
        sys.exit("usage: trim-spec.py <full-spec.json> <trimmed-spec.json>")
    main(*args)