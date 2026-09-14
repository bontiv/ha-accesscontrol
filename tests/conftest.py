"""Shared collection rules.

The tests are split in two packages:

* ``protocol/`` exercises api.py alone and needs the 'test' group;
* ``integration/`` drives a real Home Assistant and needs the
  'integration' group, which only installs on Python 3.12.
"""

from __future__ import annotations

# Skip the Home Assistant tests when their dependency group is absent, so
# that `pip install --group test && pytest` keeps working anywhere.
import importlib.util

collect_ignore: list[str] = []
if importlib.util.find_spec("pytest_homeassistant_custom_component") is None:
    collect_ignore.append("integration")
