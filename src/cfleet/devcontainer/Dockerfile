# cfleet worker container
# Based on Trail of Bits' claude-code-devcontainer
FROM mcr.microsoft.com/devcontainers/base:ubuntu-24.04

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

RUN apt-get update && apt-get install -y --no-install-recommends \
  bubblewrap \
  socat \
  fd-find \
  ripgrep \
  tmux \
  build-essential \
  jq \
  nano \
  unzip \
  vim \
  rsync \
  && apt-get clean && rm -rf /var/lib/apt/lists/*

# Node.js 22
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - && \
  apt-get install -y --no-install-recommends nodejs && \
  apt-get clean && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /workspace/repos /workspace/inbox /workspace/outbox && \
  chown -R vscode:vscode /workspace

USER vscode

# Claude Code CLI
RUN curl -fsSL https://claude.ai/install.sh | bash

ENV PATH="/home/vscode/.local/bin:$PATH"
ENV TERM=xterm-256color

WORKDIR /workspace
