"""Shared test settings.

The built-in fixture loader (/samples/load) is switched off in the product; the automated tests use it
as a fast, deterministic way to import the golden files through the normal upload path.
"""
import os

os.environ.setdefault("AVOCET_SHOW_SAMPLES", "1")
