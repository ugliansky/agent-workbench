#!/usr/bin/env bash

set -euo pipefail

dispatch_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

python3 -m unittest discover \
  -s "$dispatch_root/tests" \
  -p 'test_*.py' \
  -v
python3 -m py_compile "$dispatch_root/src/agent_dispatch.py"
bash -n "$dispatch_root/install.sh"
"$dispatch_root/tests/integration.sh"
