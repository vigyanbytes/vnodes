import json
import tempfile
import unittest
from pathlib import Path

from vnodes import chain


class ChainTests(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / "changes.jsonl"

    def fill(self, n=3):
        for i in range(n):
            chain.append(self.path, {"kind": "test", "i": i})

    def test_append_links_and_verifies(self):
        self.fill()
        entries = chain.read(self.path)
        self.assertEqual([e["seq"] for e in entries], [0, 1, 2])
        self.assertEqual(entries[0]["prev_hash"], chain.GENESIS_PREV)
        self.assertEqual(entries[1]["prev_hash"], entries[0]["hash"])
        self.assertEqual(chain.verify(self.path), [])

    def test_canonical_hash_ignores_formatting(self):
        self.assertEqual(chain.canonical_hash({"a": 1, "b": [1, 2]}),
                         chain.canonical_hash(json.loads('{ "b":[1,2],  "a":1 }')))

    def rewrite(self, fn):
        lines = self.path.read_text().splitlines()
        lines = fn(lines)
        self.path.write_text("".join(l + "\n" for l in lines))

    def test_edited_entry_detected(self):
        self.fill()
        def edit(lines):
            e = json.loads(lines[1]); e["i"] = 99; lines[1] = json.dumps(e); return lines
        self.rewrite(edit)
        self.assertTrue(any("seq 1" in e and "hash" in e for e in chain.verify(self.path)))

    def test_rehashed_entry_breaks_link(self):
        self.fill()
        def edit(lines):
            e = json.loads(lines[1]); e["i"] = 99; e.pop("hash")
            e["hash"] = chain.entry_hash(e); lines[1] = json.dumps(e); return lines
        self.rewrite(edit)
        errs = chain.verify(self.path)
        self.assertTrue(any("seq 2" in e and "prev_hash" in e for e in errs), errs)

    def test_deleted_entry_detected(self):
        self.fill()
        self.rewrite(lambda lines: [lines[0], lines[2]])
        self.assertTrue(chain.verify(self.path))

    def test_truncated_tail_detected_by_head_check(self):
        self.fill()
        head = chain.read(self.path)[-1]["hash"]
        self.rewrite(lambda lines: lines[:2])
        self.assertEqual(chain.verify(self.path), [])          # chain alone can't see truncation
        self.assertTrue(chain.verify(self.path, expect_head=head))  # git-committed head can

    def test_garbage_line_detected(self):
        self.fill(1)
        with self.path.open("a") as f:
            f.write("not json\n")
        self.assertTrue(chain.verify(self.path))


if __name__ == "__main__":
    unittest.main()
