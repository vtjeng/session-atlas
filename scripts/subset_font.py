#!/usr/bin/env python3
"""Rebuild the font files in fonts/ that generate_site.py embeds.

Download the source files named in FONTS from
https://github.com/google/fonts/tree/main/ofl (crimsonpro and jetbrainsmono)
into one directory, then run:

    uv run --with fonttools --with brotli scripts/subset_font.py DIRECTORY

Each output keeps the weight axis from 400 to 700 and the characters in
LATIN, Google Fonts' Latin subset plus the arrows. A page falls back to the
next font in its stack for any other character.
"""

import io
import os
import sys

from fontTools import subset
from fontTools.ttLib import TTFont
from fontTools.varLib import instancer

LATIN = ("U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+02C6,U+02DA,U+02DC,"
         "U+2000-206F,U+2074,U+20AC,U+2122,U+2190-2199,U+2212,U+2215")
# The mono keeps no contextual alternates or ligatures, so a command's != or
# -> shows as typed rather than as one symbol.
MONO_FEATURES = ["ccmp", "locl", "mark", "mkmk", "kern"]
FONTS = [  # source file, output file, OpenType layout features to keep
    ("CrimsonPro[wght].ttf", "CrimsonPro-Latin.woff2", ["*"]),
    ("CrimsonPro-Italic[wght].ttf", "CrimsonPro-Italic-Latin.woff2", ["*"]),
    ("JetBrainsMono[wght].ttf", "JetBrainsMono-Latin.woff2", MONO_FEATURES),
]
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fonts")


def subset_font(source, target, features):
    font = instancer.instantiateVariableFont(
        TTFont(source, lazy=False), {"wght": (400, 700)})
    # Reload the instanced font, which the subsetter cannot read in place.
    buffer = io.BytesIO()
    font.save(buffer)
    buffer.seek(0)
    font = TTFont(buffer, lazy=False)
    options = subset.Options()
    options.flavor = "woff2"
    options.layout_features = features
    options.name_IDs = ["*"]
    subsetter = subset.Subsetter(options)
    subsetter.populate(unicodes=subset.parse_unicodes(LATIN))
    subsetter.subset(font)
    subset.save_font(font, target, options)


def main():
    source_dir, = sys.argv[1:]
    for source, target, features in FONTS:
        subset_font(os.path.join(source_dir, source), os.path.join(OUT, target), features)


if __name__ == "__main__":
    main()
