import unittest

from sqlchunk.lexer import split_statements


class LexerTest(unittest.TestCase):
    def texts(self, sql):
        return [s.text.decode("utf-8") for s in split_statements(sql)]

    def test_basic(self):
        stmts = split_statements(b"SELECT 1; SELECT 2;")
        self.assertEqual([s.text for s in stmts], [b"SELECT 1", b" SELECT 2"])
        self.assertEqual([s.terminator for s in stmts], [b";", b";"])

    def test_trailing_without_terminator_is_lossless(self):
        stmts = split_statements(b"SELECT 1; SELECT 2")
        self.assertEqual(len(stmts), 2)
        self.assertEqual(stmts[1].text, b" SELECT 2")
        self.assertEqual(stmts[1].terminator, b"")

    def test_semicolon_in_single_quote(self):
        stmts = split_statements(b"INSERT INTO t VALUES ('a;b;c'); SELECT 2;")
        self.assertEqual(len(stmts), 2)
        self.assertEqual(stmts[0].text, b"INSERT INTO t VALUES ('a;b;c')")

    def test_semicolon_in_double_and_backtick_quote(self):
        stmts = split_statements(b'INSERT INTO `a;b` VALUES ("x;y"); Z;')
        self.assertEqual(len(stmts), 2)

    def test_doubled_quotes(self):
        stmts = split_statements(b"VALUES ('it''s;x'); N;")
        self.assertEqual(len(stmts), 2)
        self.assertIn(b"it''s", stmts[0].text)

    def test_backslash_escapes_in_strings(self):
        stmts = split_statements(b"VALUES ('a\\';b'); N;")
        self.assertEqual(len(stmts), 2)
        stmts = split_statements(b'VALUES ("a\\";b"); N;')
        self.assertEqual(len(stmts), 2)

    def test_line_comment_dash_dash(self):
        sql = b"SELECT 1; -- this ; is a comment\nSELECT 2;"
        stmts = split_statements(sql)
        self.assertEqual(len(stmts), 2)
        self.assertIn(b"-- this ; is a comment", stmts[1].text)

    def test_dash_dash_without_space_is_code(self):
        stmts = split_statements(b"SELECT 1- -2; SELECT 2;")
        self.assertEqual(len(stmts), 2)
        stmts = split_statements(b"SELECT a--1\nFROM dual; X;")
        self.assertEqual(len(stmts), 2)

    def test_hash_comment(self):
        stmts = split_statements(b"SELECT 1; # hash ; comment\nSELECT 2;")
        self.assertEqual(len(stmts), 2)

    def test_block_comment(self):
        stmts = split_statements(b"/* one ; two */ SELECT 1; /* x; */ SELECT 2;")
        self.assertEqual(len(stmts), 2)
        self.assertTrue(stmts[0].text.startswith(b"/* one ; two */ SELECT 1"))

    def test_dollar_quote_with_tag(self):
        sql = b"CREATE FUNCTION f() RETURNS text AS $$ BEGIN; x; END $$ LANGUAGE sql; SELECT 1;"
        stmts = split_statements(sql)
        self.assertEqual(len(stmts), 2)
        self.assertTrue(stmts[0].text.endswith(b"LANGUAGE sql"))

    def test_line_continuation_backslash_newline(self):
        sql = b"INSERT INTO t VALUES('ok') \\\n; SELECT 2;"
        stmts = split_statements(sql)
        self.assertEqual(len(stmts), 2)
        self.assertIn(b"\\\n", stmts[0].text)

    def test_delimiter_switch(self):
        sql = (
            b"CREATE TABLE t(a INT);\n"
            b"DELIMITER $$\n"
            b"CREATE TRIGGER trg BEGIN SELECT 1; SELECT 2; END$$\n"
            b"DELIMITER ;\n"
            b"INSERT INTO t VALUES(1);"
        )
        stmts = split_statements(sql)
        self.assertEqual(len(stmts), 3)
        self.assertIn(b"SELECT 1; SELECT 2;", stmts[1].text)
        self.assertEqual(stmts[1].terminator, b"$$")
        self.assertEqual(stmts[2].terminator, b";")
        self.assertNotIn(b"DELIMITER", stmts[0].text + stmts[1].text + stmts[2].text)

    def test_empty_and_comment_only_statements_kept(self):
        stmts = split_statements(b";;; /* c; */ ;")
        self.assertEqual(len(stmts), 4)
        self.assertFalse(any(s.has_code for s in stmts[:3]))
        self.assertFalse(stmts[3].has_code)  # block comment only

    def test_utf8_content_preserved(self):
        payload = "VALUES ('中文；分号;é');".encode("utf-8")
        stmts = split_statements(payload + b" Z;")
        self.assertEqual(len(stmts), 2)
        self.assertEqual(stmts[0].text, payload[:-1])

    def test_offsets_round_trip(self):
        sql = b"A; BB; CCC;"
        stmts = split_statements(sql)
        for s in stmts:
            self.assertEqual(s.text, sql[s.start:s.end])

    def test_unterminated_string_does_not_crash(self):
        stmts = split_statements(b"SELECT 'unterminated; rest")
        self.assertEqual(len(stmts), 1)
        self.assertIn(b"unterminated; rest", stmts[0].text)


if __name__ == "__main__":
    unittest.main()
