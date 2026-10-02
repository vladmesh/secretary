"""Container ownership shared by the head guard and board test producer."""

import re
from collections.abc import Mapping

TEST_BOARD_LABEL = "ummanu.test-board"
PRODUCTION_BOARD_LABEL = "ummanu.production-board"


def is_test_container(labels: object) -> bool:
    """A production marker always protects a container, even if also test labelled."""
    if not isinstance(labels, Mapping) or PRODUCTION_BOARD_LABEL in labels:
        return False
    owner = labels.get(TEST_BOARD_LABEL)
    return isinstance(owner, str) and re.fullmatch(r"[1-9][0-9]*", owner) is not None
