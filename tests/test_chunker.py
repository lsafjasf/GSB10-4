import unittest

from sqlchunk.chunker import build_chunks, split_and_chunk
from sqlchunk.lexer import split_statements


class ChunkerTest(unittest.TestCase):
    def test_single_chunk_when_under_limit(self):
        chunks = split_and_chunk(b"SELECT 1; SELECT 2;", 1 << 20)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].statement_count, 2)
        self.assertFalse(chunks[0].oversized)

    def test_packing_respects_limit(self):
        sql = b"".join(b"SELECT %d; " % i for i in range(10))
        chunks = split_and_chunk(sql, 30)
        for c in chunks:
            self.assertLessEqual(c.size, 30)
        self.assertEqual(sum(c.statement_count for c in chunks), 10)

    def test_oversized_statement_gets_own_chunk(self):
        big = b"INSERT INTO t VALUES('" + b"x" * 5000 + b"');"
        sql = b"SELECT 1; " + big + b" SELECT 2;"
        chunks = split_and_chunk(sql, 100)
        self.assertEqual(len(chunks), 3)
        self.assertTrue(chunks[1].oversized)
        self.assertEqual(chunks[1].statement_count, 1)
        self.assertGreater(chunks[1].size, 100)

    def test_chunk_boundaries_never_split_statements(self):
        sql = (
            b"INSERT INTO t VALUES('a;b'); /* c; */ SELECT 2; "
            b"VALUES ('x'';y'); SELECT 3;"
        )
        stmts = split_statements(sql)
        chunks = build_chunks(stmts, 25)
        covered = []
        for c in chunks:
            covered.extend(range(c.start_stmt, c.end_stmt))
        self.assertEqual(covered, list(range(len(stmts))))
        for c in chunks:
            self.assertEqual(c.text, b"".join(s.text for s in c.statements))

    def test_empty_script(self):
        self.assertEqual(split_and_chunk(b"", 100), [])
        self.assertEqual(split_and_chunk(b"   \n  ", 100), [])

    def test_invalid_chunk_size(self):
        with self.assertRaises(ValueError):
            split_and_chunk(b"SELECT 1;", 0)

    def test_sha256_stable(self):
        import hashlib

        chunks = split_and_chunk(b"SELECT 1; SELECT 2;", 8)
        for c in chunks:
            self.assertEqual(c.sha256, hashlib.sha256(c.text).hexdigest())


if __name__ == "__main__":
    unittest.main()
