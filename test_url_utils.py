import unittest

from url_utils import safe_internal_redirect


class SafeInternalRedirectTests(unittest.TestCase):
    def test_allows_internal_path_and_query(self):
        self.assertEqual(safe_internal_redirect("/ok?x=1&y=2", "/fallback"), "/ok?x=1&y=2")

    def test_rejects_external_and_ambiguous_values(self):
        for value in (
            "/\\evil.com",
            "//evil.com",
            "https://evil.com",
            "/%2f%2fevil.com",
            "/%5cevil.com",
            "/ok\nnext",
            "",
            None,
        ):
            with self.subTest(value=value):
                self.assertEqual(safe_internal_redirect(value, "/fallback"), "/fallback")


if __name__ == "__main__":
    unittest.main()
