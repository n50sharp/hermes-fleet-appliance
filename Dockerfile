FROM python:3.11-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    bash \
    curl \
    git \
    sqlite3 \
    ca-certificates \
    procps \
    jq \
    build-essential \
    libffi-dev \
    libatomic1 \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user hermes
RUN groupadd -g 1000 hermes && \
    useradd -u 1000 -g hermes -m -s /bin/bash hermes

# Copy appliance seed assets
COPY profiles/ /opt/appliance/profiles/
COPY mission-control/ /opt/appliance/mission-control/
COPY wiki/ /opt/appliance/wiki/
COPY obsidian-vault/ /opt/appliance/obsidian-vault/
COPY entrypoint.sh /entrypoint.sh

RUN chmod +x /entrypoint.sh && chown -R hermes:hermes /opt/appliance /entrypoint.sh

USER hermes
ENV HOME=/home/hermes
ENV PATH="/home/hermes/.local/bin:${PATH}"
WORKDIR /home/hermes

# Install Hermes Agent using official repository installer
RUN git clone --depth 1 https://github.com/NousResearch/hermes-agent.git /home/hermes/.hermes/hermes-agent && \
    cd /home/hermes/.hermes/hermes-agent && \
    (./scripts/install.sh --non-interactive || pip install --user hermes-agent || true)

# BUG FIX: docker-compose.yml mounts a named volume at /home/hermes/.hermes for data
# persistence. An empty volume shadows the hermes-agent source tree installed above at
# container start, breaking the `hermes` command on every `docker compose up`. Snapshot
# the fully-installed tree to a path OUTSIDE the mounted volume so entrypoint.sh can
# restore it into the (possibly empty) volume on first boot.
RUN mkdir -p /opt/appliance/hermes-agent-seed && \
    cp -a /home/hermes/.hermes/hermes-agent /opt/appliance/hermes-agent-seed/hermes-agent

# Pre-create directory structures
RUN mkdir -p /home/hermes/.hermes/profiles /home/hermes/wiki /home/hermes/Obsidian-vaults/note-vault /home/hermes/agent-mission-control /home/hermes/fleet-workspaces

EXPOSE 51763 9119

ENTRYPOINT ["/entrypoint.sh"]
