#!/usr/bin/env bash
# install.sh — stand up the companion engine: tpmem KB, the gateway + dispatcher, and
# the two default persistent agents (companion + curator).
#
# The default interface is the local gateway webapp. External transports
# (Telegram/email) are optional and stay OFF — see config.example.yaml if you want them.
#
# Safe to re-run: existing token/registry/config are left in place.
set -euo pipefail

ENGINE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOME_DIR="$HOME"
TPMEM_DIR="$HOME_DIR/.tpmem"
AGENTOS_DIR="$TPMEM_DIR/agent-os"
DB="${TPMEM_DB:-$TPMEM_DIR/kb.db}"
REGISTRY="$AGENTOS_DIR/registry.json"
TOKEN_FILE="$AGENTOS_DIR/gateway.token"
SKILLS_DIR="$HOME_DIR/.claude/skills"
UNIT_DIR="$HOME_DIR/.config/systemd/user"
PORT="${GATEWAY_PORT:-7364}"
COLLAB_DOC="${COLLAB_DOC:-$HOME_DIR/agentic-collaboration/README.md}"

say() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m warn:\033[0m %s\n' "$*"; }

# ── 1. prerequisites ─────────────────────────────────────────────────────────
say "Checking prerequisites"
missing=0
for c in python3 sqlite3 tmux node npm; do
  if ! command -v "$c" >/dev/null 2>&1; then warn "missing: $c"; missing=1; fi
done
if ! command -v claude >/dev/null 2>&1; then
  warn "the 'claude' CLI is not on PATH — install/authenticate Claude Code before starting agents"
fi
[ "$missing" -eq 0 ] || { echo "Install the missing tools above and re-run."; exit 1; }

# ── 2. directories ───────────────────────────────────────────────────────────
say "Creating runtime directories under $TPMEM_DIR"
mkdir -p "$AGENTOS_DIR" "$TPMEM_DIR/agent-state" "$TPMEM_DIR/media" "$SKILLS_DIR" "$UNIT_DIR"

# ── 2b. collaboration guide ──────────────────────────────────────────────────
# spawn-agent attaches this "how to work with the user" guide to the top of every
# agent's context on each spawn/rotate (path override: COLLAB_DOC). Seed it from the
# copy shipped in this repo if the user hasn't provided their own. If you maintain it
# as a git repo at that path, spawn-agent will git-pull it before each spawn.
if [ ! -s "$COLLAB_DOC" ]; then
  say "Seeding collaboration guide -> $COLLAB_DOC"
  mkdir -p "$(dirname "$COLLAB_DOC")"
  cp "$ENGINE_DIR/COLLABORATION.md" "$COLLAB_DOC"
else
  say "Collaboration guide already present ($COLLAB_DOC) — keeping it"
fi

# ── 3. schema ────────────────────────────────────────────────────────────────
say "Applying schema to $DB"
sqlite3 "$DB" < "$ENGINE_DIR/schema.sql"

# ── 4. gateway token ─────────────────────────────────────────────────────────
if [ ! -s "$TOKEN_FILE" ]; then
  say "Generating gateway bearer token -> $TOKEN_FILE"
  if command -v openssl >/dev/null 2>&1; then openssl rand -hex 32 > "$TOKEN_FILE"
  else head -c32 /dev/urandom | od -An -tx1 | tr -d ' \n' > "$TOKEN_FILE"; fi
  chmod 600 "$TOKEN_FILE"
else
  say "Gateway token already present ($TOKEN_FILE) — keeping it"
fi

# ── 5. runtime registry (2 default agents) ───────────────────────────────────
if [ ! -s "$REGISTRY" ]; then
  say "Seeding registry -> $REGISTRY"
  sed "s#__ENGINE_DIR__#$ENGINE_DIR#g" "$ENGINE_DIR/registry.example.json" > "$REGISTRY"
else
  say "Registry already present ($REGISTRY) — keeping it"
fi

# ── 6. skills ────────────────────────────────────────────────────────────────
say "Installing skills into $SKILLS_DIR"
mkdir -p "$SKILLS_DIR/companion" "$SKILLS_DIR/curate-memory"
cp "$ENGINE_DIR/skills/companion/SKILL.md"     "$SKILLS_DIR/companion/SKILL.md"
cp "$ENGINE_DIR/skills/curate-memory/SKILL.md" "$SKILLS_DIR/curate-memory/SKILL.md"

# ── 7. gateway deps ──────────────────────────────────────────────────────────
say "Installing gateway dependencies (npm install)"
( cd "$ENGINE_DIR/gateway" && npm install --silent )

# ── 8. systemd user units ────────────────────────────────────────────────────
say "Installing systemd --user units into $UNIT_DIR"
for unit in companion-gateway companion-dispatcher companion-agents companion-transport-bridge; do
  sed "s#__ENGINE_DIR__#$ENGINE_DIR#g" "$ENGINE_DIR/systemd/$unit.service" > "$UNIT_DIR/$unit.service"
done
systemctl --user daemon-reload
say "Enabling gateway + dispatcher + agents (transport bridge stays disabled)"
systemctl --user enable --now companion-gateway.service
systemctl --user enable --now companion-dispatcher.service
systemctl --user enable --now companion-agents.service

# ── 9. daily curation cron ───────────────────────────────────────────────────
CRON_LINE="0 9 * * * PATH=$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin TPMEM_DB=$DB $ENGINE_DIR/bin/queue-job curator \"/curate-memory — daily curation pass\" >/dev/null 2>&1"
if command -v crontab >/dev/null 2>&1; then
  if ! crontab -l 2>/dev/null | grep -qF "$ENGINE_DIR/bin/queue-job curator"; then
    say "Adding daily curate-memory cron (09:00)"
    ( crontab -l 2>/dev/null; echo "$CRON_LINE" ) | crontab -
  else
    say "Curation cron already present — keeping it"
  fi
else
  warn "no crontab available — add this yourself to run daily curation:"
  echo "    $CRON_LINE"
fi

# ── 10. put memory tools on PATH (callable by bare name) ──────────────────────
# kb / kb-note / agent-recover ship in bin/. Symlink them into ~/.local/bin so agents
# can call them by name (`kb entity ...`) instead of by full path — matching their docs.
say "Wiring memory tools (kb, kb-note, agent-recover) onto PATH (~/.local/bin)"
mkdir -p "$HOME_DIR/.local/bin"
for t in kb kb-note agent-recover; do
  ln -sf "$ENGINE_DIR/bin/$t" "$HOME_DIR/.local/bin/$t"
done
for rc in "$HOME_DIR/.bashrc" "$HOME_DIR/.profile"; do
  [ -f "$rc" ] || touch "$rc"
  grep -q '.local/bin' "$rc" 2>/dev/null || echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$rc"
done

# ── done ─────────────────────────────────────────────────────────────────────
TOKEN="$(cat "$TOKEN_FILE")"
cat <<EOF

$(say "Install complete.")

  Gateway (your control deck):   http://localhost:$PORT
  Bearer token (paste to log in): $TOKEN

  The token is your ONLY access gate to the gateway — anyone with it can drive Claude
  Code on this machine. Keep it secret; it lives at $TOKEN_FILE (chmod 600). The port
  is bound on 0.0.0.0 but should only be reachable on a trusted LAN/VPN, never the open
  internet.

  Useful commands:
    $ENGINE_DIR/bin/dispatch-ctl status          # see agent states + pending queues
    systemctl --user status companion-dispatcher  # dispatcher health
    tmux attach -t companion                      # watch the companion session live

  Persistent-across-logout: run  'loginctl enable-linger $USER'  so the services and
  agents survive you logging out.

  First run: the first 'claude' launch in a new project dir may show a trust prompt.
  If the agents don't come online, attach with tmux (above) and accept it once.

  Optional external transport (Telegram/email) is OFF. To enable, see README + copy
  config.example.yaml -> ~/.config/tpmem-daemon/config.yaml and enable the bridge.

  Add more agents later: see docs/SELF_EXPANSION.md.
EOF
