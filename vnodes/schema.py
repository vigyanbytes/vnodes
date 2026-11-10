"""Minimal JSON Schema 2020-12 validator (the subset nodes.schema.json uses) + semantic checks.

Stdlib only by design: no jsonschema package, no venv. Supported keywords: $ref (local #/$defs, or the
absolute $id of another *.schema.json in the same directory, optionally with a #/ fragment), allOf, type,
enum, const, required, properties, additionalProperties, items, uniqueItems, pattern, minLength, maxLength,
minimum, maximum, format(date-time). Annotation `x-deprecated-enum` ({value: hint}) produces warnings.
"""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = ROOT / "schema" / "nodes.schema.json"
NODE_SCHEMA_PATH = ROOT / "schema" / "node.schema.json"
_REGISTRY = {}  # $id -> schema document, filled by load_schema from the schema's directory

_DT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$")
_TYPES = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
}


def load_schema(path=None):
    """Load a schema and register every *.schema.json beside it by $id (so cross-file $refs resolve)."""
    path = Path(path or SCHEMA_PATH)
    for sib in sorted(path.parent.glob("*.schema.json")):
        doc = json.loads(sib.read_text())
        if "$id" in doc:
            _REGISTRY[doc["$id"]] = doc
    return json.loads(path.read_text())


def load_node_schema(path=None):
    return load_schema(path or NODE_SCHEMA_PATH)


def _resolve(ref, root):
    """Return (target schema, its document root)."""
    if ref.startswith("#"):
        doc, frag = root, ref[1:]
    else:
        uri, _, frag = ref.partition("#")
        if uri not in _REGISTRY:
            load_schema()  # the repo's own schema directory
        if uri not in _REGISTRY:
            raise ValueError(f"unknown schema $ref {uri!r} (not in the schema directory)")
        doc = _REGISTRY[uri]
    node = doc
    for part in [p for p in frag.split("/") if p]:
        node = node[part]
    return node, doc


def validate(value, sch, root=None, path="$"):
    root = root if root is not None else sch
    errs = []
    if "$ref" in sch:
        target, troot = _resolve(sch["$ref"], root)
        errs += validate(value, target, troot, path)
    for sub in sch.get("allOf", []):
        errs += validate(value, sub, root, path)
    if "type" in sch:
        types = sch["type"] if isinstance(sch["type"], list) else [sch["type"]]
        if not any(_TYPES[t](value) for t in types):
            return errs + [f"{path}: expected {'|'.join(types)}, got {type(value).__name__}"]
    if "const" in sch and value != sch["const"]:
        errs.append(f"{path}: must be {sch['const']!r}")
    if "enum" in sch and value not in sch["enum"]:
        errs.append(f"{path}: {value!r} not one of {sch['enum']}")
    if isinstance(value, str):
        if "pattern" in sch and not re.search(sch["pattern"], value):
            errs.append(f"{path}: {value!r} does not match {sch['pattern']}")
        if "minLength" in sch and len(value) < sch["minLength"]:
            errs.append(f"{path}: shorter than {sch['minLength']}")
        if "maxLength" in sch and len(value) > sch["maxLength"]:
            errs.append(f"{path}: longer than {sch['maxLength']}")
        if sch.get("format") == "date-time" and not _DT.match(value):
            errs.append(f"{path}: {value!r} is not an RFC 3339 date-time")
    if _TYPES["number"](value):
        if "minimum" in sch and value < sch["minimum"]:
            errs.append(f"{path}: below minimum {sch['minimum']}")
        if "maximum" in sch and value > sch["maximum"]:
            errs.append(f"{path}: above maximum {sch['maximum']}")
    if isinstance(value, dict):
        for req in sch.get("required", []):
            if req not in value:
                errs.append(f"{path}: missing required property {req!r}")
        props = sch.get("properties", {})
        addl = sch.get("additionalProperties", True)
        for k, v in value.items():
            if k in props:
                errs += validate(v, props[k], root, f"{path}.{k}")
            elif addl is False:
                errs.append(f"{path}: unknown property {k!r}")
            elif isinstance(addl, dict):
                errs += validate(v, addl, root, f"{path}.{k}")
    if isinstance(value, list):
        if "items" in sch:
            for i, v in enumerate(value):
                errs += validate(v, sch["items"], root, f"{path}[{i}]")
        if sch.get("uniqueItems"):
            seen = [json.dumps(v, sort_keys=True) for v in value]
            if len(seen) != len(set(seen)):
                errs.append(f"{path}: items are not unique")
    return errs


def semantic_errors(doc):
    """Rules JSON Schema cannot express."""
    errs = []
    nodes = doc.get("nodes") if isinstance(doc, dict) else None
    if not isinstance(nodes, list):
        return errs
    seen_id, seen_name = set(), set()
    for i, n in enumerate(nodes):
        if not isinstance(n, dict):
            continue
        where = f"$.nodes[{i}]"
        if n.get("id") in seen_id:
            errs.append(f"{where}: duplicate id {n.get('id')!r}")
        seen_id.add(n.get("id"))
        if n.get("name") in seen_name:
            errs.append(f"{where}: duplicate name {n.get('name')!r}")
        seen_name.add(n.get("name"))
        extra = set(n.get("primary_for") or []) - set(n.get("roles") or [])
        if extra:
            errs.append(f"{where}: primary_for {sorted(extra)} not in roles")
        if n.get("status") == "retired" and "retired_at" not in n:
            errs.append(f"{where}: retired node needs retired_at")
        if n.get("status") == "active" and ("retired_at" in n or "retired_reason" in n):
            errs.append(f"{where}: active node must not carry retired_at/retired_reason")
        errs += [f"{where}: {e}" for e in node_semantic_errors(n)]
    return errs


def node_semantic_errors(n):
    errs = []
    if n.get("os") == "windows-git-bash" and n.get("shell") not in (None, "git-bash"):
        errs.append(f"os windows-git-bash implies shell git-bash, not {n.get('shell')!r} (use os=windows)")
    return errs


def _deprecated_os():
    return (load_node_schema()["properties"]["os"].get("x-deprecated-enum") or {})


def warnings(doc):
    """Non-fatal findings: deprecated values still accepted for backward compatibility."""
    dep = _deprecated_os()
    out = []
    for i, n in enumerate((doc or {}).get("nodes") or []):
        if isinstance(n, dict) and n.get("os") in dep:
            out.append(f"$.nodes[{i}] ({n.get('id')}): os {n['os']!r} is deprecated: {dep[n['os']]}")
    return out


def export_node(n):
    """A node as consumers see it (v2.1): deprecated os values mapped to os + shell."""
    n = dict(n)
    if n.get("os") == "windows-git-bash":
        n["os"] = "windows"
        n.setdefault("shell", "git-bash")
    return n


def validate_node(rec, node_schema=None):
    """A single node record (kit request, discovery proposal) against node.schema.json."""
    sch = node_schema or load_node_schema()
    return validate(rec, sch) + node_semantic_errors(rec if isinstance(rec, dict) else {})


def validate_inventory(doc, schema_doc=None):
    sch = schema_doc or load_schema()
    return validate(doc, sch) + semantic_errors(doc)
