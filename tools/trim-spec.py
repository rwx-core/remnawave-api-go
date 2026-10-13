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
# trailing comment honest — it is the reason the path is in this list.
OPERATIONS = {
    # user lifecycle
    "/api/users": "create, update, list",
    "/api/users/{userId}": "read, delete",
    "/api/users/{userId}/actions/enable": "enable",
    "/api/users/{userId}/actions/disable": "disable",
    "/api/users/{userId}/actions/revoke": "revoke subscription",
    "/api/users/by-username/{username}": "lookup by username",
    # device hygiene
    "/api/hwid/devices/{userId}": "list devices",
    # squad catalogue
    "/api/internal-squads": "list squads",
}

ROOT = pathlib.Path(__file__).resolve().parent.parent

# Parameters whose declared type the vendor got wrong. In 3.4.5 the panel
# specification types these identifiers as `number` with no integer format, so
# ogen renders a float into the path: GET /api/users/12345 is sent as
# /api/users/12345.0000000000 and the panel rejects it. The 3.4.4
# specification said `integer`, and the panel takes an integer (rwx-store's
# adapter passes int64 and the live panel accepts it), so this is a vendor
# regression that we correct here instead of propagating.
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


def drop_union_responses(methods, path, dropped):
    """Remove responses whose body is a union, recording each removal."""
    for method, operation in methods.items():
        if not isinstance(operation, dict):
            continue
        responses = operation.get("responses") or {}
        for status, response in list(responses.items()):
            for media in (response.get("content") or {}).values():
                schema = media.get("schema") or {}
                if "oneOf" in schema or "anyOf" in schema:
                    del responses[status]
                    dropped.append(f"{method.upper():6} {path} -> {status}")
                    break


def main(source, target):
    doc = json.loads(pathlib.Path(source).read_text())
    schemas = (doc.get("components") or {}).get("schemas") or {}
    paths = doc.get("paths") or {}

    absent = sorted(set(OPERATIONS) - set(paths))
    if absent:
        sys.exit(
            "These operations are absent from " + str(source) + ":\n  "
            + "\n  ".join(absent)
            + "\nThe panel changed its API. Update OPERATIONS before regenerating."
        )

    keep = set()
    kept_paths = {}
    for path in OPERATIONS:
        methods = paths[path]
        kept_paths[path] = methods
        collect_refs(methods, keep)

    dropped = []
    corrected = 0
    for path, methods in kept_paths.items():
        drop_union_responses(methods, path, dropped)
        corrected += apply_integer_overrides(methods)

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
    print(f"union error responses dropped: {len(dropped)}")
    print(f"integer param corrections: {corrected}")
    for line in dropped:
        print(f"  {line}")


if __name__ == "__main__":
    args = sys.argv[1:]
    if len(args) != 2:
        sys.exit("usage: trim-spec.py <full-spec.json> <trimmed-spec.json>")
    main(*args)