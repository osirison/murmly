"""What the window frame is made of, as plain data the tests can read."""

from __future__ import annotations

# The sections the header's navigation switches between, in the order the
# canvas draws them. `id` is stable (QML and tests find items by it); `title`
# is the label a person reads.
SECTIONS: tuple[dict[str, str], ...] = (
    {"id": "home", "title": "Home"},
    {"id": "settings", "title": "Settings"},
    {"id": "health", "title": "Health"},
)
