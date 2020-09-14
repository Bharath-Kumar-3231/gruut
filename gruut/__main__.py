#!/usr/bin/env python3
"""Command-line interface to gruut"""
import argparse
import dataclasses
import json
import logging
import os
import sys
from pathlib import Path

import jsonlines
import pydash
import yaml

from .utils import env_constructor

# -----------------------------------------------------------------------------

_LOGGER = logging.getLogger("gruut")

_DIR = Path(__file__).parent
_DATA_DIR = _DIR / "data"

# -----------------------------------------------------------------------------


def main():
    """Main entry point"""
    # Expand environment variables in string value
    yaml.SafeLoader.add_constructor("!env", env_constructor)

    args = get_args()

    if args.debug:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.INFO)

    _LOGGER.debug(args)

    lang_dir = _DATA_DIR / args.language
    assert lang_dir.is_dir(), "Unsupported language"

    # Load configuration
    config_path = lang_dir / "language.yml"
    assert config_path.is_file(), f"Missing {config_path}"

    # Set environment variable for config loading
    os.environ["config_dir"] = str(config_path.parent)
    with open(config_path, "r") as config_file:
        config = yaml.safe_load(config_file)

    args.func(config, args)


# -----------------------------------------------------------------------------


def do_tokenize(config, args):
    """
    Split lines from stdin into sentences, tokenize and clean.

    Prints a line of JSON for each sentence.
    """
    from .toksen import Tokenizer

    tokenizer = Tokenizer(config)

    writer = jsonlines.Writer(sys.stdout)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        for sentence in tokenizer.tokenize(line):
            writer.write(dataclasses.asdict(sentence))


# -----------------------------------------------------------------------------


def do_phonemize(config, args):
    """
    Reads JSONL from stdin with "clean_words" property.

    Looks up or guesses phonetic pronuncation(s) for all clean words.

    Prints a line of JSON for each input line.
    """
    from .phonemize import Phonemizer

    phonemizer = Phonemizer(config)

    writer = jsonlines.Writer(sys.stdout)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        sentence_obj = json.loads(line)
        clean_words = sentence_obj["clean_words"]

        sentence_prons = phonemizer.phonemize(clean_words)
        sentence_obj["pronunciations"] = sentence_prons

        # Pick first pronunciation for each word
        first_pron = []
        for word_prons in sentence_prons:
            if word_prons:
                first_pron.append(word_prons[0])

        sentence_obj["pronunciation"] = first_pron

        # Create string of first pronunciation
        sentence_obj["pronunciation_text"] = " ".join(
            " ".join(word_pron) for word_pron in first_pron
        )

        # Print back out with extra info
        writer.write(sentence_obj)


# -----------------------------------------------------------------------------


def do_phones_to_phonemes(config, args):
    """Transform/group phones in a pronuncation into language phonemes"""
    from . import Phonemes

    phonemes_path = Path(pydash.get(config, "language.phonemes"))

    with open(phonemes_path, "r") as phonemes_file:
        phonemes = Phonemes.from_text(phonemes_file)

    writer = jsonlines.Writer(sys.stdout)
    for line in sys.stdin:
        line = line.strip()
        if line:
            line_phonemes = phonemes.split(line, keep_stress=args.keep_stress)
            phonemes_list = [p.text for p in line_phonemes]

            writer.write(
                {
                    "language": args.language,
                    "raw_text": line,
                    "phonemes_text": " ".join(phonemes_list),
                    "phonemes_list": phonemes_list,
                    "phonemes": [p.to_dict() for p in line_phonemes],
                }
            )


# -----------------------------------------------------------------------------


def get_args() -> argparse.Namespace:
    """Parse command-line arguments"""
    parser = argparse.ArgumentParser(prog="gruut")
    parser.add_argument("language", help="Language code (e.g., en-us)")

    # Create subparsers for each sub-command
    sub_parsers = parser.add_subparsers()
    sub_parsers.required = True
    sub_parsers.dest = "command"

    # --------
    # tokenize
    # --------
    tokenize_parser = sub_parsers.add_parser(
        "tokenize", help="Sentencize/tokenize raw text, clean, and expand numbers"
    )
    tokenize_parser.set_defaults(func=do_tokenize)

    # ---------
    # phonemize
    # ---------
    phonemize_parser = sub_parsers.add_parser(
        "phonemize", help="Look up or guess word pronunciations from JSONL sentences"
    )
    phonemize_parser.set_defaults(func=do_phonemize)

    # ---------------
    # phones2phonemes
    # ---------------
    phones2phonemes_parser = sub_parsers.add_parser(
        "phones2phonemes", help="Group phonetic pronunciation into language phonemes"
    )
    phones2phonemes_parser.set_defaults(func=do_phones_to_phonemes)
    phones2phonemes_parser.add_argument(
        "--keep-stress",
        action="store_true",
        help="Keep primary/secondary stress markers",
    )

    # Shared arguments
    for sub_parser in [tokenize_parser, phonemize_parser, phones2phonemes_parser]:
        sub_parser.add_argument(
            "--debug", action="store_true", help="Print DEBUG messages to console"
        )

    return parser.parse_args()


# -----------------------------------------------------------------------------


if __name__ == "__main__":
    main()
