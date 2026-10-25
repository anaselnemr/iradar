#!/usr/bin/env bash
# One-time preparation of a fresh Ubuntu 24.04 VPS for iRadar. Run as root.
# Safe to re-run: every step checks before it acts.
set -euo pipefail

# Docker, from Docker's own installer
if ! command -v docker >/dev/null; then
  curl -fsSL https://get.docker.com | sh
fi

# security updates install themselves
apt-get update -q
DEBIAN_FRONTEND=noninteractive apt-get install -y -q unattended-upgrades ufw git
dpkg-reconfigure -f noninteractive unattended-upgrades

# firewall: SSH and the web, nothing else
ufw allow OpenSSH
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable

# 4 GB of swap: a cushion for Chromium's peaks during the Sunday check
if ! swapon --show | grep -q /swapfile; then
  fallocate -l 4G /swapfile && chmod 600 /swapfile
  mkswap /swapfile && swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

# code, data and settings
# The code arrives from the developer's machine (git archive over ssh), so the
# server needs no access to the private GitHub repo.
mkdir -p /opt/iradar/data /opt/iradar/repo
touch /opt/iradar/.env && chmod 600 /opt/iradar/.env
echo "ready: put the data in /opt/iradar/data and the settings in /opt/iradar/.env,"
echo "then: cd /opt/iradar/repo/deploy/vps && docker compose up -d --build"
