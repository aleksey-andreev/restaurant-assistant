#!/usr/bin/env bash
set -euo pipefail

if ! command -v sudo >/dev/null 2>&1; then
  echo "sudo is required for installation steps."
  exit 1
fi

TARGET_USER="${SUDO_USER:-${USER}}"
if [[ -z "${TARGET_USER}" ]]; then
  echo "Cannot detect target non-root user."
  exit 1
fi

if command -v apt-get >/dev/null 2>&1; then
  sudo apt-get update
  sudo apt-get install -y ca-certificates curl gnupg lsb-release git

  sudo install -m 0755 -d /etc/apt/keyrings
  if [[ ! -f /etc/apt/keyrings/docker.gpg ]]; then
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    sudo chmod a+r /etc/apt/keyrings/docker.gpg
  fi

  . /etc/os-release
  ARCH="$(dpkg --print-architecture)"
  echo \
    "deb [arch=${ARCH} signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
    | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null

  sudo apt-get update
  sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  sudo systemctl enable --now docker
  sudo usermod -aG docker "${TARGET_USER}"
else
  echo "Unsupported package manager. Install Docker Engine and Docker Compose Plugin manually."
  exit 1
fi

echo
echo "Docker installed. Next steps:"
echo "1) Re-login as ${TARGET_USER} (or run: newgrp docker) to apply docker group."
echo "2) Copy project to VM."
echo "3) Create production .env with remote PostgreSQL DATABASE_URL."
echo "4) Run as non-root user: docker compose -f docker-compose.prod.yml up -d --build"
