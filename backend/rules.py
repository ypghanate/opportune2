"""Input rules shared by sign-up and the profile form. The frontend repeats them for instant feedback."""
from uk_data import DEGREES, UNIVERSITIES

NAME_PUNCT = set("'’.-")


def full_name(value: str) -> str:
    """At least two words. Each starts with a letter, uses only letters and ' - . and is 2+ characters
    (an initial needs its full stop: "J."). Returns the name with single spaces."""
    parts = (value or "").split()
    if len(parts) < 2:
        raise ValueError("Enter your full name (first and last name)")
    for p in parts:
        if not (p[0].isalpha() and all(c.isalpha() or c in NAME_PUNCT for c in p)):
            raise ValueError("Names can only contain letters, spaces, hyphens, apostrophes and full stops")
        if len(p) < 2:
            raise ValueError("Write each part of your name in full (an initial needs a full stop, like J.)")
    name = " ".join(parts)
    if len(name) > 100:
        raise ValueError("That name is too long")
    return name


def university(value: str) -> str:
    if value not in UNIVERSITIES:
        raise ValueError("Choose your university from the list")
    return value


def degree(value: str) -> str:
    if value not in DEGREES:
        raise ValueError("Choose your degree from the list")
    return value
