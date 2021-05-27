#!/usr/bin/env python3
"""Tests for RegexTokenizer class"""
import unittest

from gruut.lang import EnglishTokenizer


class EnglishTokenizerTestCase(unittest.TestCase):
    """Test cases for EnglishTokenizer class"""

    def test_unclean_text(self):
        """Test text with lots of noise"""
        tokenizer = EnglishTokenizer()

        text = "IT’S <a> 'test' (seNtEnce) for $100, dr., & [ I ] ## like ## it 100%!"
        sentences = list(tokenizer.tokenize(text))
        self.assertEqual(1, len(sentences))
        sentence = sentences[0]

        self.assertEqual(
            [
                "it's",
                "a",
                "test",
                "sentence",
                "for",
                "one",
                "hundred",
                "dollars",
                ",",
                "doctor",
                ",",
                "and",
                "i",
                "like",
                "it",
                "one",
                "hundred",
                "percent",
                "!",
            ],
            [t.text for t in sentence.tokens],
        )


# -----------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main()
