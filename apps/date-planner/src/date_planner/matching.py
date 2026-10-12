"""Whether two names, or two places, are the same venue.

Claude doesn't spell a venue the same way in every chat — "Nobu", "Nobu
Malibu", "Nobu Malibu Restaurant" — so a venue is found by what it plausibly
is, not by an exact string. Kept free of imports from the rest of the app so
both the database layer and the Places lookup can use it.
"""

import re
import unicodedata

# A pin dropped from the venue's own site and Google's can disagree by a car
# park's width; a different branch is further than this.
SAME_PLACE_M = 300.0

_FILLER = {"the", "restaurant", "bar", "and", "cafe", "kitchen"}


def _words(name: str) -> set[str]:
    plain = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    words = set(re.findall(r"[a-z0-9]+", plain.lower().replace("&", " and ")))
    return (words - _FILLER) or words


def names_match(ours: str, theirs: str) -> bool:
    """Whether Google's name is plausibly our venue's.

    Every significant word of the shorter name has to appear in the longer:
    "Nobu" is "Nobu Malibu", "A Tí" is "A TÍ Restaurant", but "Bestia" is
    not "Bavel".
    """
    a, b = _words(ours), _words(theirs)
    if not a or not b:
        return False
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    return shorter <= longer
