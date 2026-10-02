"""Validate the product memory pack shipped by this checkout."""

from __future__ import annotations

import unittest
from pathlib import Path

from ummanu.memory.pack import load_product_pack


class ShippedMemoryPackTests(unittest.TestCase):
    def test_shipped_product_pack_is_valid(self) -> None:
        repository_root = Path(__file__).resolve().parents[1]

        load_product_pack(repository_root)
