"""Small shared typing seam that keeps prompt transport independent of delivery policy."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

RunJson = Callable[[list[str]], dict[str, Any]]
