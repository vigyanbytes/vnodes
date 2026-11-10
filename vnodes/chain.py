"""Append-only, hash-chained change log (changes.jsonl).

Each entry carries seq, prev_hash and hash = sha256(canonical JSON of the entry without `hash`).
Editing, reordering or deleting a middle entry breaks the chain. Truncating the tail is caught by
comparing with an expected head (the last entry's after_hash must match nodes.json, and git keeps
history).
"""
import hashlib
import json
from pathlib import Path

GENESIS_PREV = "0" * 64


def canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def canonical_hash(obj):
    return hashlib.sha256(canonical(obj).encode()).hexdigest()


def entry_hash(entry):
    return canonical_hash({k: v for k, v in entry.items() if k != "hash"})


def read(path):
    path = Path(path)
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def head(path):
    entries = read(path)
    return entries[-1] if entries else None


def append(path, entry):
    path = Path(path)
    last = head(path)
    e = dict(entry)
    e["seq"] = 0 if last is None else last["seq"] + 1
    e["prev_hash"] = GENESIS_PREV if last is None else last["hash"]
    e.pop("hash", None)
    e["hash"] = entry_hash(e)
    with path.open("a") as f:
        f.write(canonical(e) + "\n")
        f.flush()
    return e


def verify(path, expect_head=None):
    errs = []
    path = Path(path)
    if not path.exists():
        return [f"{path.name}: missing"]
    prev, expected_seq, last = GENESIS_PREV, 0, None
    for lineno, line in enumerate(path.read_text().splitlines(), 1):
        if not line.strip():
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            errs.append(f"{path.name} line {lineno}: not valid JSON")
            continue
        seq = e.get("seq")
        if seq != expected_seq:
            errs.append(f"{path.name} seq {seq}: expected seq {expected_seq} (entry missing or reordered)")
        if e.get("prev_hash") != prev:
            errs.append(f"{path.name} seq {seq}: prev_hash does not link to the previous entry")
        if e.get("hash") != entry_hash(e):
            errs.append(f"{path.name} seq {seq}: hash mismatch (entry edited)")
        prev, expected_seq, last = e.get("hash"), (seq if isinstance(seq, int) else expected_seq) + 1, e
    if expect_head is not None and (last is None or last.get("hash") != expect_head):
        errs.append(f"{path.name}: head is not the expected {expect_head[:12]}… (tail truncated or replaced)")
    return errs
