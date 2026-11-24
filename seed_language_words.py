#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Seed the LanguageWord table from the content workers' CSV files.

Usage:
    DATABASE_URL=sqlite:///tmp/lang.db python3 seed_language_words.py [dir]

Loads lang_seed_medical.csv, lang_seed_engineering.csv and
lang_seed_general.csv from [dir] (default: this script's directory),
each into its own track. Missing files are skipped with a warning —
they are produced by the content workers. Uses the shared loader
import_language_words_csv from App.py, so admin upload and seeding
apply exactly the same validation and dedup rules.

Requires DATABASE_URL (or falls back to App.py's default sqlite file).
No AI calls, no secrets.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import App  # noqa: E402

SEED_FILES = (
    ("lang_seed_medical.csv", "medical"),
    ("lang_seed_engineering.csv", "engineering"),
    ("lang_seed_general.csv", "general"),
)


def main():
    base_dir = sys.argv[1] if len(sys.argv) > 1 else HERE
    app = App.app
    total = {"imported": 0, "skipped": 0, "skipped_invalid": 0}
    with app.app_context():
        for filename, track in SEED_FILES:
            path = os.path.join(base_dir, filename)
            if not os.path.exists(path):
                print(f"SKIP {filename}: file not found in {base_dir}")
                continue
            result = App.import_language_words_csv(path, default_track=track)
            for key in total:
                total[key] += result[key]
            print(f"{filename} ({track}): imported={result['imported']} "
                  f"skipped={result['skipped']} "
                  f"skipped_invalid={result['skipped_invalid']}")
        counts = App._word_counts_by_track()
        print(f"TOTAL: imported={total['imported']} skipped={total['skipped']} "
              f"skipped_invalid={total['skipped_invalid']}")
        print(f"DB now holds: {counts} (total={sum(counts.values())})")


if __name__ == "__main__":
    main()
