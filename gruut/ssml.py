#!/usr/bin/env python3
import operator
import re
import sys
import typing
import unittest
import xml.etree.ElementTree as etree
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path

import babel
import babel.numbers
import dateparser
import networkx as nx
from num2words import num2words

from .pos import PartOfSpeechTagger
from .const import REGEX_MATCH, REGEX_PATTERN, REGEX_TYPE, Token, TokenFeatures
from .utils import grouper, maybe_compile_regex

# -----------------------------------------------------------------------------

DEFAULT_MAJOR_BREAKS = set(".?!")
DEFAULT_MINOR_BREAKS = set(",;:")
DEFAULT_SPLIT_PATTERN = re.compile("(\s+)")
DEFAULT_NON_WORD_PATTERN = re.compile(r"^(\W|_)+$")

# GRAPH_TYPE = nx.DiGraph
# NODE_TYPE = int
# NODE_ELEMENT = "element"


class InterpretAs(str, Enum):
    SPELL_OUT = "spell-out"
    DATE = "date"
    NUMBER = "number"
    CURRENCY = "currency"


class InterpretAsFormat(str, Enum):
    NUMBER_CARDINAL = "cardinal"
    NUMBER_ORDINAL = "ordinal"
    NUMBER_DIGITS = "digits"
    NUMBER_YEAR = "year"

    DATE_DMY = "dmy"
    DATE_MDY = "mdy"
    DATE_YMD = "ymd"
    DATE_YM = "ym"
    DATE_MY = "my"
    DATE_MD = "md"
    DATE_DM = "dm"
    DATE_Y = "y"


# -----------------------------------------------------------------------------


@dataclass
class Node:
    node: int
    element: typing.Optional[etree.Element] = None
    voice: str = ""


@dataclass
class Break(Node):
    time: str = ""


@dataclass
class Sub(Node):
    alias: str = ""


@dataclass
class Word(Node):
    text: str = ""
    interpret_as: typing.Union[str, InterpretAs] = ""
    format: typing.Union[str, InterpretAsFormat] = ""

    number: typing.Optional[Decimal] = None
    date: typing.Optional[datetime] = None
    currency: typing.Optional[str] = None

    pos: typing.Optional[str] = None


@dataclass
class Sentence(Node):
    pass


@dataclass
class Paragraph(Node):
    pass


@dataclass
class Speak(Node):
    pass


# -----------------------------------------------------------------------------


class SSMLTokenizer:
    @dataclass
    class EndElement:
        element: etree.Element

    def __init__(
        self,
        lang: str = "en_US",
        babel_locale: typing.Optional[str] = None,
        num2words_lang: typing.Optional[str] = None,
        dateparser_lang: typing.Optional[str] = None,
        split_pattern: REGEX_TYPE = DEFAULT_SPLIT_PATTERN,
        join_str: str = " ",
        keep_whitespace: bool = True,
        pos_model: typing.Optional[typing.Union[str, Path]] = None,
        replacements: typing.Optional[
            typing.Sequence[typing.Tuple[REGEX_TYPE, str]]
        ] = None,
    ):
        if babel_locale is None:
            babel_locale = lang

        if num2words_lang is None:
            num2words_lang = lang

        if dateparser_lang is None:
            # en_US -> en
            dateparser_lang = lang.split("_")[0]

        self.lang = lang
        self.babel_locale = babel_locale
        self.num2words_lang = num2words_lang
        self.dateparser_lang = dateparser_lang

        self.keep_whitespace = keep_whitespace

        self.default_currency = "USD"
        self.currencies = {"$": "USD"}

        self.split_pattern = split_pattern
        self.join_str = join_str

        # Sorted by decreasing length
        self.currency_symbols = sorted(
            self.currencies, key=operator.length_hint, reverse=True
        )

        self.replacements = [
            (maybe_compile_regex(p), r) for p, r in (replacements or [])
        ]

        self.pos_tagger: typing.Optional[PartOfSpeechTagger] = None
        if pos_model is not None:
            self.pos_tagger = PartOfSpeechTagger(pos_model)

    def pre_tokenize(self, text: str) -> str:
        for pattern, replacement in self.replacements:
            text = pattern.sub(replacement, text)

        return text

    def tokenize(self, text: str, add_speak_tag: bool = True):
        if add_speak_tag and (not text.lstrip().startswith("<")):
            # Wrap in <speak> tag
            text = f"<speak>{text}</speak>"

        root_element = etree.fromstring(text)
        graph = nx.DiGraph()
        self.tokenize_etree(root_element, graph)

    def tokenize_etree(self, root_element: etree.Element, graph):
        # TODO: Remove namespaces
        assert root_element.tag == "speak", "Root must be <speak>"

        target_stack: typing.List[Node] = []
        target: typing.Optional[Node] = None
        root: typing.Optional[Speak] = None

        voice_stack: typing.List[str] = []
        say_as_stack: typing.List[typing.Tuple[str, str]] = []

        def scope_kwargs(target_class):
            scope = {}
            if voice_stack:
                scope["voice"] = voice_stack[-1]

            if say_as_stack and (target_class is Word):
                scope["interpret_as"], scope["format"] = say_as_stack[-1]

            return scope

        def find_parent(classes):
            while target_stack and (not isinstance(target_stack[-1], classes)):
                target_stack.pop()

            if target_stack:
                return target_stack[-1]

            return root

        for elem_or_text in SSMLTokenizer.text_and_elements(root_element):
            # print(elem_or_text, file=sys.stderr)
            if isinstance(elem_or_text, str):
                text = typing.cast(str, elem_or_text)
                target = find_parent((Sentence, Paragraph, Speak))

                if isinstance(target, Speak):
                    # Ensure paragraph
                    p_node = Paragraph(node=len(graph), **scope_kwargs(Paragraph))
                    graph.add_node(p_node.node, data=p_node)

                    graph.add_edge(target.node, p_node.node)
                    target_stack.append(p_node)
                    target = p_node

                if isinstance(target, Paragraph):
                    # Ensure sentence
                    s_node = Sentence(node=len(graph), **scope_kwargs(Sentence))
                    graph.add_node(s_node.node, data=s_node)

                    graph.add_edge(target.node, s_node.node)
                    target_stack.append(s_node)
                    target = s_node

                if isinstance(target, Sentence):
                    # Interpret as single sentence
                    self.pipeline_tokenize(
                        text,
                        self.split_pattern,
                        target,
                        graph,
                        scope_kwargs=scope_kwargs(Word),
                    )
                else:
                    # May be multiple sentences
                    pass

            elif isinstance(elem_or_text, SSMLTokenizer.EndElement):
                # End of an element (e.g., </s>)
                end_elem = typing.cast(SSMLTokenizer.EndElement, elem_or_text)
                end_tag = end_elem.element.tag

                if end_tag == "voice":
                    if voice_stack:
                        voice_stack.pop()
                elif end_tag == "say-as":
                    say_as_stack.pop()
                else:
                    while target_stack and (
                        target_stack[-1].element != end_elem.element
                    ):
                        target_stack.pop()

                    if target_stack:
                        target = target_stack[-1]
                    else:
                        target = root
            else:
                # Start of an element (e.g., <p>)
                elem = typing.cast(etree.Element, elem_or_text)

                # TODO: namespaces
                if elem.tag == "speak":
                    # Root <speak>
                    assert root is None
                    root = Speak(node=len(graph), element=elem)
                    graph.add_node(root.node, data=root)
                    target_stack.append(root)
                    target = root
                elif elem.tag == "voice":
                    # Set voice scope
                    voice_name = elem.attrib["name"]
                    voice_stack.append(voice_name)
                elif elem.tag == "p":
                    # Paragraph
                    target = find_parent(Speak)

                    p_node = Paragraph(
                        node=len(graph), element=elem, **scope_kwargs(Paragraph)
                    )
                    graph.add_node(p_node.node, data=p_node)
                    graph.add_edge(target.node, p_node.node)
                    target_stack.append(p_node)
                    target = p_node
                elif elem.tag == "s":
                    # Sentence
                    target = find_parent((Paragraph, Speak))

                    # Ensure paragraph
                    if isinstance(target, Speak):
                        p_node = Paragraph(node=len(graph), **scope_kwargs(Paragraph))
                        graph.add_node(p_node.node, data=p_node)

                        graph.add_edge(target.node, p_node.node)
                        target_stack.append(p_node)
                        target = p_node

                    s_node = Sentence(node=len(graph), element=elem)
                    graph.add_node(s_node.node, data=s_node)
                    graph.add_edge(target.node, s_node.node)
                    target_stack.append(s_node)
                    target = s_node
                elif elem.tag == "break":
                    # Break
                    target = find_parent((Sentence, Paragraph, Speak))
                    break_node = Break(
                        node=len(graph), element=elem, time=elem.attrib.get("time", "")
                    )
                    graph.add_node(break_node.node, data=break_node)
                    graph.add_edge(target.node, break_node.node)
                elif elem.tag == "say-as":
                    say_as_stack.append(
                        (
                            elem.attrib.get("interpret-as", ""),
                            elem.attrib.get("format", ""),
                        )
                    )

        assert root is not None

        # Transform text into known classes
        self.pipeline_transform(self.transform_number, root, graph)
        self.pipeline_transform(self.transform_date, root, graph)
        self.pipeline_transform(self.transform_currency, root, graph)

        # Verbalize known classes
        self.pipeline_transform(self.verbalize_number, root, graph)
        self.pipeline_transform(self.verbalize_date, root, graph)
        self.pipeline_transform(self.verbalize_currency, root, graph)

        # Add part-of-speech tags
        if self.pos_tagger is not None:
            # Predict tags for entire sentence
            words = [
                w
                for w in self.leaves(graph, root, skip_seen=True)
                if isinstance(w, Word)
            ]
            pos_tags = self.pos_tagger([w.text for w in words])
            for word, pos in zip(words, pos_tags):
                word.pos = pos

        SSMLTokenizer.print_graph(graph, root.node)

    def leaves(self, graph, node, skip_seen=False, seen=None):
        if skip_seen and (seen is None):
            seen = set()

        if graph.out_degree(node.node) == 0:
            if not skip_seen or (node.node not in seen):
                yield node

                if seen is not None:
                    seen.add(node.node)
        else:
            for next_node_n in graph.successors(node.node):
                next_node = graph.nodes[next_node_n]["data"]
                yield from self.leaves(graph, next_node, skip_seen=skip_seen, seen=seen)

    def pipeline_tokenize(self, text, pattern, parent_node, graph, scope_kwargs=None):
        if scope_kwargs is None:
            scope_kwargs = {}

        for non_ws, ws in grouper(pattern.split(text), 2):
            word = non_ws
            if self.keep_whitespace:
                word += ws or ""

            if not word:
                continue

            word_node = Word(node=len(graph), text=word, **scope_kwargs)
            graph.add_node(word_node.node, data=word_node)
            graph.add_edge(parent_node.node, word_node.node)

    def pipeline_transform(self, transform_func, parent_node, graph, skip_seen=True):
        for leaf_node in list(self.leaves(graph, parent_node, skip_seen=skip_seen)):
            transform_func(leaf_node, graph)

    def transform_number(self, node, graph, babel_locale: typing.Optional[str] = None):
        if not isinstance(node, Word):
            return

        word = typing.cast(Word, node)
        if word.interpret_as and (word.interpret_as != InterpretAs.NUMBER):
            return

        babel_locale = babel_locale or self.babel_locale

        try:
            # Try to parse as a number
            # This is important to handle thousand/decimal separators correctly.
            number = babel.numbers.parse_decimal(word.text, locale=babel_locale)
            word.interpret_as = InterpretAs.NUMBER
            word.number = number
        except ValueError:
            pass

    def transform_date(self, node, graph):
        if not isinstance(node, Word):
            return

        word = typing.cast(Word, node)
        if word.interpret_as and (word.interpret_as != InterpretAs.DATE):
            return

        dateparser_kwargs = {
            "settings": {"STRICT_PARSING": True},
            "languages": [self.dateparser_lang],
        }

        date = dateparser.parse(word.text, **dateparser_kwargs)
        if date is not None:
            word.interpret_as = InterpretAs.DATE
            word.date = date

    def transform_currency(
        self, node, graph, babel_locale: typing.Optional[str] = None
    ):

        if not isinstance(node, Word):
            return

        word = typing.cast(Word, node)
        if word.interpret_as and (word.interpret_as != InterpretAs.CURRENCY):
            return

        babel_locale = babel_locale or self.babel_locale

        for currency_symbol in self.currency_symbols:
            if word.text.startswith(currency_symbol):
                num_str = word.text[len(currency_symbol) :]
                try:
                    # Try to parse as a number
                    # This is important to handle thousand/decimal separators correctly.
                    number = babel.numbers.parse_decimal(num_str, locale=babel_locale)
                    word.interpret_as = InterpretAs.CURRENCY
                    word.currency = currency_symbol
                    word.number = number
                    break
                except ValueError:
                    pass

    def verbalize_number(self, node, graph, **num2words_kwargs):
        if not isinstance(node, Word):
            return

        word = typing.cast(Word, node)
        if (word.interpret_as != InterpretAs.NUMBER) or (word.number is None):
            return

        if "lang" not in num2words_kwargs:
            num2words_kwargs["lang"] = self.num2words_lang

        decimal_nums = [word.number]

        if word.format == InterpretAsFormat.NUMBER_CARDINAL:
            num2words_kwargs["to"] = "cardinal"
        elif word.format == InterpretAsFormat.NUMBER_ORDINAL:
            num2words_kwargs["to"] = "ordinal"
        elif word.format == InterpretAsFormat.NUMBER_YEAR:
            num2words_kwargs["to"] = "year"
        elif word.format == InterpretAsFormat.NUMBER_DIGITS:
            num2words_kwargs["to"] = "cardinal"
            decimal_nums = [Decimal(d) for d in str(word.number.to_integral_value())]

        for decimal_num in decimal_nums:
            num_has_frac = (decimal_num % 1) != 0

            # num2words uses the number as an index sometimes, so it *has* to be
            # an integer, unless we're doing currency.
            if num_has_frac:
                final_num = float(decimal_num)
            else:
                final_num = int(decimal_num)

            # Convert to words (e.g., 100 -> one hundred)
            num_str = num2words(final_num, **num2words_kwargs)

            # Remove all non-word characters
            num_str = re.sub(r"\W", " ", num_str).strip()

            # Split into separate words
            for part_str, sep_str in grouper(self.split_pattern.split(num_str), 2):
                number_word_text = part_str
                if self.keep_whitespace:
                    number_word_text += sep_str or ""

                if not number_word_text:
                    continue

                if self.keep_whitespace and (not number_word_text.endswith(" ")):
                    number_word_text += " "

                number_word = Word(node=len(graph), text=number_word_text)
                graph.add_node(number_word.node, data=number_word)
                graph.add_edge(word.node, number_word.node)

    def verbalize_date(
        self, node, graph, babel_locale: typing.Optional[str] = None, **num2words_kwargs
    ):
        if not isinstance(node, Word):
            return

        word = typing.cast(Word, node)
        if (word.interpret_as != InterpretAs.DATE) or (word.date is None):
            return

        babel_locale = babel_locale or self.babel_locale
        if "lang" not in num2words_kwargs:
            num2words_kwargs["lang"] = self.num2words_lang

        date = word.date
        date_format = (word.format or InterpretAsFormat.DATE_MDY).strip().upper()
        day_str = ""
        month_str = ""
        year_str = ""

        if "M" in date_format:
            month_str = babel.dates.format_date(date, "MMMM", locale=babel_locale)

        if num2words_kwargs is None:
            num2words_kwargs = {}

        if "lang" not in num2words_kwargs:
            num2words_kwargs["lang"] = babel_locale

        if "D" in date_format:
            num2words_kwargs["to"] = "ordinal"
            day_str = num2words(date.day, **num2words_kwargs)

        if "Y" in date_format:
            num2words_kwargs["to"] = "year"
            year_str = num2words(date.year, **num2words_kwargs)

        # Transform into format string
        # MDY -> {M} {D} {Y}
        date_format_str = self.join_str.join(f"{{{c}}}" for c in date_format)
        date_str = date_format_str.format(M=month_str, D=day_str, Y=year_str)

        # Split into separate words
        for part_str, sep_str in grouper(self.split_pattern.split(date_str), 2):
            date_word_text = part_str
            if self.keep_whitespace:
                date_word_text += sep_str or ""

            if not date_word_text:
                continue

            if self.keep_whitespace and (not date_word_text.endswith(" ")):
                date_word_text += " "

            date_word = Word(node=len(graph), text=date_word_text)
            graph.add_node(date_word.node, data=date_word)
            graph.add_edge(word.node, date_word.node)

    def verbalize_currency(
        self,
        node,
        graph,
        default_currency: typing.Optional[str] = None,
        currencies: typing.Optional[typing.Dict[str, str]] = None,
        **num2words_kwargs,
    ):
        if not isinstance(node, Word):
            return

        word = typing.cast(Word, node)
        if (
            (word.interpret_as != InterpretAs.CURRENCY)
            or (word.currency is None)
            or (word.number is None)
        ):
            return

        default_currency = default_currency or self.default_currency
        decimal_num = word.number

        # True if number has non-zero fractional part
        num_has_frac = (decimal_num % 1) != 0

        if num2words_kwargs is None:
            num2words_kwargs = {}

        num2words_kwargs["to"] = "currency"

        # Name of currency (e.g., USD)
        currency_name = default_currency
        if currencies:
            currency_name = currencies.get(word.currency, default_currency)

        num2words_kwargs["currency"] = currency_name

        # Custom separator so we can remove 'zero cents'
        num2words_kwargs["separator"] = "|"

        num_str = num2words(float(decimal_num), **num2words_kwargs)

        # Post-process currency words
        if num_has_frac:
            # Discard num2words separator
            num_str = num_str.replace("|", "")
        else:
            # Remove 'zero cents' part
            num_str = num_str.split("|", maxsplit=1)[0]

        # Remove all non-word characters
        num_str = re.sub(r"\W", self.join_str, num_str).strip()

        # Split into separate words
        for part_str, sep_str in grouper(self.split_pattern.split(num_str), 2):
            currency_word_text = part_str
            if self.keep_whitespace:
                currency_word_text += sep_str or ""

            if not currency_word_text:
                continue

            if self.keep_whitespace and (not currency_word_text.endswith(" ")):
                currency_word_text += " "

            currency_word = Word(node=len(graph), text=currency_word_text)
            graph.add_node(currency_word.node, data=currency_word)
            graph.add_edge(word.node, currency_word.node)

    @staticmethod
    def text_and_elements(element):
        yield element

        # Text before any elements
        text = element.text.strip() if element.text is not None else ""
        if text:
            yield text

        for child in element:
            yield from SSMLTokenizer.text_and_elements(child)

        # Text after any elements
        tail = element.tail.strip() if element.tail is not None else ""
        if tail:
            yield tail

        yield SSMLTokenizer.EndElement(element)

    @staticmethod
    def print_graph(g, n, s: str = "-"):
        n_data = g.nodes[n]["data"]
        print(s, n, n_data, file=sys.stderr)
        for n2 in g.successors(n):
            SSMLTokenizer.print_graph(g, n2, s + "-")


# -----------------------------------------------------------------------------


class SSMLTokenizerTestCase(unittest.TestCase):
    def test1(self):
        tokenizer = SSMLTokenizer()
        tokenizer.tokenize(
            "<speak>This is a test. <p><s>These are.</s><s>Two sentences.</s></p></speak>"
        )

    def test2(self):
        tokenizer = SSMLTokenizer()
        tokenizer.tokenize("<speak>1/2/2022 1,234 $50.32</speak>")

    def test3(self):
        tokenizer = SSMLTokenizer()
        tokenizer.tokenize(
            '<speak><say-as interpret-as="number" format="ordinal">12</say-as></speak>'
        )

    def test4(self):
        tokenizer = SSMLTokenizer(pos_model="/home/hansenm/opt/gruut/gruut/data/en-us/pos/model.crf")
        tokenizer.tokenize("This is a <break /> $50 test")