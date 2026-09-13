import os
import tempfile
import unittest

from discord_username_checker.generator import (
    ALLOWED_CHARS,
    build_candidates,
    count_pattern,
    generate_all,
    generate_from_pattern,
    read_wordlist,
    resolve_charset,
    validate_username,
)


class ValidateUsernameTests(unittest.TestCase):
    def test_valid_names(self):
        for name in ("abcd", "a_b.", "1234", "ab", "a" * 32, "x.y_"):
            self.assertIsNone(validate_username(name), name)

    def test_length(self):
        self.assertIsNotNone(validate_username("a"))
        self.assertIsNotNone(validate_username("a" * 33))

    def test_forbidden_characters(self):
        for name in ("ABCD", "ab-c", "ab c", "abé", "ab@c"):
            self.assertIsNotNone(validate_username(name), name)

    def test_consecutive_dots(self):
        self.assertIsNotNone(validate_username("a..b"))
        self.assertIsNone(validate_username("a.b."))

    def test_reserved_words(self):
        self.assertIsNotNone(validate_username("here"))
        self.assertIsNotNone(validate_username("discord"))
        self.assertIsNotNone(validate_username("xhere"))


class CharsetTests(unittest.TestCase):
    def test_named_charsets(self):
        self.assertEqual(len(resolve_charset("letters")), 26)
        self.assertEqual(len(resolve_charset("alnum")), 36)
        self.assertEqual(resolve_charset("FULL"), ALLOWED_CHARS)

    def test_custom_charset_deduplicated(self):
        self.assertEqual(resolve_charset("aabbc"), "abc")

    def test_invalid_charset(self):
        with self.assertRaises(ValueError):
            resolve_charset("ab-")
        with self.assertRaises(ValueError):
            resolve_charset("")


class GenerationTests(unittest.TestCase):
    def test_generate_all_count(self):
        names = list(generate_all(2, "abc"))
        self.assertEqual(len(names), 9)
        self.assertEqual(names[0], "aa")
        self.assertEqual(names[-1], "cc")

    def test_pattern(self):
        names = list(generate_from_pattern("a?z", "xy"))
        self.assertEqual(names, ["axz", "ayz"])
        self.assertEqual(count_pattern("a??z", "abc"), 9)
        self.assertEqual(list(generate_from_pattern("ABCD", "xy")), ["abcd"])

    def test_wordlist(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "liste.txt")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("# commentaire\nAbcd\n\nefgh\nabcd\n")
            self.assertEqual(read_wordlist(path), ["abcd", "efgh"])

    def test_build_candidates_full(self):
        iterator, total = build_candidates(length=3, charset="ab")
        self.assertEqual(total, 8)
        self.assertEqual(len(list(iterator)), 8)

    def test_build_candidates_limit_and_shuffle(self):
        iterator, total = build_candidates(length=2, charset="abc", shuffle=True, seed=1, limit=4)
        names = list(iterator)
        self.assertEqual(total, 4)
        self.assertEqual(len(names), 4)
        other, _ = build_candidates(length=2, charset="abc", shuffle=True, seed=1, limit=4)
        self.assertEqual(names, list(other))  # reproductible avec la même graine

    def test_build_candidates_explicit(self):
        iterator, total = build_candidates(explicit=["ABCD", "abcd", " efgh "])
        self.assertEqual(list(iterator), ["abcd", "efgh"])
        self.assertEqual(total, 2)

    def test_build_candidates_negative_limit(self):
        with self.assertRaises(ValueError):
            build_candidates(length=2, charset="ab", limit=-1)


if __name__ == "__main__":
    unittest.main()
