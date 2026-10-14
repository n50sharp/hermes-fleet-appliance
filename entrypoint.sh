#!/usr/bin/env bash
set -e

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
WIKI_DIR="${CREW_WIKI_DIR:-$HOME/wiki}"
OBSIDIAN_DIR="$HOME/Obsidian-vaults/note-vault"
MC_DIR="$HOME/agent-mission-control"
APPLIANCE_SEED="/opt/appliance"

echo "=== Booting Hermes Fleet Appliance ==="

# 0. Restore hermes-agent installation if the mounted volume shadowed it (fresh/empty
#    hermes_data volume on first `docker compose up`, or any other wipe of $HERMES_HOME).
if [ ! -d "$HERMES_HOME/hermes-agent" ] && [ -d "$APPLIANCE_SEED/hermes-agent-seed/hermes-agent" ]; then
    echo "Restoring hermes-agent installation into \$HERMES_HOME (volume was empty)..."
    mkdir -p "$HERMES_HOME"
    cp -a "$APPLIANCE_SEED/hermes-agent-seed/hermes-agent" "$HERMES_HOME/hermes-agent"
fi

# 1. Initialize user directories
mkdir -p "$HERMES_HOME/profiles" "$WIKI_DIR" "$OBSIDIAN_DIR" "$MC_DIR" "$HOME/fleet-workspaces"

# 2. Seed profiles if not already present
for p in default dev dev2 scout scribe reach; do
    target_p="$HERMES_HOME/profiles/$p"
    if [ ! -d "$target_p" ]; then
        echo "Seeding profile $p..."
        mkdir -p "$target_p"
        if [ -d "$APPLIANCE_SEED/profiles/$p" ]; then
            cp -r "$APPLIANCE_SEED/profiles/$p/"* "$target_p/" 2>/dev/null || true
        fi
    fi
done

# Root default profile config if needed
if [ ! -f "$HERMES_HOME/SOUL.md" ] && [ -d "$APPLIANCE_SEED/profiles/default" ]; then
    echo "Seeding root default profile..."
    cp -r "$APPLIANCE_SEED/profiles/default/"* "$HERMES_HOME/" 2>/dev/null || true
fi

# 3. Seed Wiki if empty
if [ ! -f "$WIKI_DIR/WIKI.md" ] && [ -d "$APPLIANCE_SEED/wiki" ]; then
    echo "Seeding blank wiki..."
    cp -r "$APPLIANCE_SEED/wiki/"* "$WIKI_DIR/" 2>/dev/null || true
    if [ ! -d "$WIKI_DIR/.git" ]; then
        cd "$WIKI_DIR"
        git init -b main
        git config user.name "Hermes Appliance"
        git config user.email "appliance@hermes.local"
        git add .
        git commit -m "chore: initialize clean wiki" || true
    fi
fi

# 4. Seed Obsidian vault if empty
if [ ! -d "$OBSIDIAN_DIR/.obsidian" ] && [ -d "$APPLIANCE_SEED/obsidian-vault" ]; then
    echo "Seeding blank Obsidian vault..."
    cp -r "$APPLIANCE_SEED/obsidian-vault/"* "$OBSIDIAN_DIR/" 2>/dev/null || true
    if [ ! -d "$OBSIDIAN_DIR/.git" ]; then
        cd "$OBSIDIAN_DIR"
        git init -b main
        git config user.name "Hermes Appliance"
        git config user.email "appliance@hermes.local"
        git add .
        git commit -m "chore: initialize clean obsidian vault" || true
    fi
fi

# 5. Seed Mission Control if empty
if [ ! -f "$MC_DIR/server.py" ] && [ -d "$APPLIANCE_SEED/mission-control" ]; then
    echo "Seeding Mission Control platform..."
    cp -r "$APPLIANCE_SEED/mission-control/"* "$MC_DIR/" 2>/dev/null || true
fi

# 6. Start Mission Control in background
if [ -f "$MC_DIR/server.py" ]; then
    echo "Starting Mission Control server on port ${PORT:-51763}..."
    cd "$MC_DIR"
    export PORT="${PORT:-51763}"
    export HOST="${HOST:-0.0.0.0}"
    python3 "$MC_DIR/server.py" 2>&1 | tee /tmp/mission-control.log &
    MC_PID=$!
    echo "Mission Control running with PID $MC_PID"
fi

# 7. Execute passed command or keep alive
if [ "$#" -gt 0 ]; then
    exec "$@"
else
    echo "==================================================="
    echo "Hermes Fleet Appliance is ready!"
    echo "Access Mission Control at http://localhost:${PORT:-51763}"
    echo "Run 'docker exec -it <container> hermes setup' to add your API keys."
    echo "==================================================="
    trap "kill $MC_PID 2>/dev/null; exit 0" SIGINT SIGTERM
    wait $MC_PID
fi
