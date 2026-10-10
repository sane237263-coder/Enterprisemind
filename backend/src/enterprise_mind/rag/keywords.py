"""The same deterministic Unicode/identifier/CJK tokenization is used on both sides."""

import re
import unicodedata

WORDS = re.compile(r"[A-Za-z0-9_]+(?:[.\-/][A-Za-z0-9_]+)*|[\u3400-\u9fff]+|[^\W_]", re.UNICODE)
CAMEL = re.compile(r"[A-Z]+(?=[A-Z][a-z]|\d|\b)|[A-Z]?[a-z]+|\d+")


def normalize(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def terms(text: str) -> list[str]:
    result: list[str] = []
    for match in WORDS.finditer(unicodedata.normalize("NFKC", text)):
        word = match.group()
        if "\u3400" <= word[0] <= "\u9fff":
            # Character bigrams preserve unknown enterprise vocabulary without a mutable dictionary.
            result.extend(word[i : i + 2] for i in range(max(1, len(word) - 1)))
        else:
            result.append(word.casefold())
            if re.search(r"[_./-]|[a-z][A-Z]|[A-Za-z]\d|\d[A-Za-z]", word):
                result.extend(part.casefold() for part in CAMEL.findall(word))
    return result


def search_tokens(text: str) -> str:
    return " ".join(terms(text))


def query_expression(text: str) -> str:
    # Only program-generated quoted lexemes enter to_tsquery; query syntax stays inert.
    tokens = list(dict.fromkeys(terms(text)))[:128]
    return " | ".join("'" + term.replace("'", "''") + "'" for term in tokens)
