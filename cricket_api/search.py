"""Keyword search over the names a person would type: teams, events, venues, cities, seasons and competition codes.

A search is a few keywords separated by spaces. Each keyword is matched without regard to case as a piece of a name, so "leeds" finds "Headingley, Leeds" and "2019"
finds both the 2019 and the 2019/20 seasons. Every keyword has to match at least one of the fields the route searches, and the order of the keywords does not matter.
A keyword is plain text: a percent sign, an underscore and a backslash mean themselves. There is no spelling correction, no word stemming and no guessing what a
sentence means. A name that is not spelled the way the data spells it finds nothing.

The text is normalised (Unicode NFKC) and bounded: at most MAX_QUERY characters, MAX_KEYWORDS keywords and MAX_KEYWORD characters in each. Every keyword is a bound
parameter, and the fields come from a list written in the code, never from the request.
"""
import unicodedata

from cricket_api.errors import ApiError
from cricket_api.http import escape_like

MAX_QUERY = 120
MAX_KEYWORDS = 8
MAX_KEYWORD = 40


def keywords(text: str, parameter: str = "q") -> list[str]:
    """The distinct keywords of a search, in the order typed, as they will be matched. Raises a 422 for an empty search or one past a bound."""
    words = []
    for word in unicodedata.normalize("NFKC", text).split():
        if len(word) > MAX_KEYWORD:
            raise ApiError(422, f"A search word can be at most {MAX_KEYWORD} characters", parameter=parameter)
        if word.lower() not in [w.lower() for w in words]:
            words.append(word)
    if not words:
        raise ApiError(422, "The search text is empty", parameter=parameter)
    if len(words) > MAX_KEYWORDS:
        raise ApiError(422, f"A search can have at most {MAX_KEYWORDS} words", parameter=parameter)
    return words


def clause(text: str, columns: tuple[str, ...], parameter: str = "q") -> tuple[str, list]:
    """A SQL condition that holds when every keyword is a piece of at least one of the columns, and its parameters. The columns are written in the code."""
    parts, params = [], []
    for word in keywords(text, parameter):
        parts.append("(" + " OR ".join(f"{column} ILIKE ? ESCAPE '\\'" for column in columns) + ")")
        params += [f"%{escape_like(word)}%"] * len(columns)
    return " AND ".join(parts), params
