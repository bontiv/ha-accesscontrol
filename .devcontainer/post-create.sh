#!/usr/bin/env bash
# Provision the development container.
#
# The 'integration' dependency group pulls in Home Assistant itself, so the
# whole test suite and the `hass` command are available afterwards.
set -euo pipefail

# pip installs console scripts into ~/.local/bin here, because the interpreter
# directory is root-owned. devcontainer.json puts that on PATH for terminals;
# do the same now so the checks below can find them.
export PATH="$HOME/.local/bin:$PATH"

echo "Installing development dependencies..."
# The image ships a pip older than 25.1, which does not understand PEP 735
# dependency groups.
python -m pip install --upgrade --quiet "pip>=25.1"
python -m pip install --quiet --group integration

# Assigning first means set -e aborts on a missing command, instead of
# printing an empty version and carrying on.
python_version="$(python --version)"
pytest_version="$(python -m pytest --version)"
hass_version="$(hass --version)"

cat <<EOF

${python_version}
${pytest_version}
Home Assistant ${hass_version}

Ready:
  pytest             run the whole suite (protocol + Home Assistant layer)
  pytest tests/protocol   protocol only, no Home Assistant needed
  scripts/develop    start Home Assistant on http://localhost:8123
EOF
