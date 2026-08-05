#!/bin/bash
set -euo pipefail

# Solo en sesiones remotas (Claude Code on the web) - en local el usuario ya
# gestiona su propio entorno.
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "$CLAUDE_PROJECT_DIR"

# Un venv aislado evita el conflicto con el paquete `cryptography` del sistema
# (panic de su binding en Rust) que se vio varias veces al usar python3 directo.
if [ ! -d ".venv" ]; then
  python3 -m venv .venv
fi

.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt uvicorn

echo "export PATH=\"$CLAUDE_PROJECT_DIR/.venv/bin:\$PATH\"" >> "$CLAUDE_ENV_FILE"
