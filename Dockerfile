# iRadar - Flask dashboard + Playwright capture engine, one container.
#
# Built on Playwright's own image rather than a plain python base. Chromium
# needs about sixty system libraries (nss, atk, cups, pango, libdrm, xkbcommon
# and friends); installing them by hand is the step that quietly breaks on
# every buildpack, and the version of that list has to match the version of
# Playwright driving it. This image ships both, already matched.
#
# The tag MUST track the playwright version in requirements.txt. A mismatch
# fails at runtime with "Executable doesn't exist", not at build time.
FROM mcr.microsoft.com/playwright/python:v1.61.0-noble

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    # behind Railway's router / a Cloudflare tunnel, both of which terminate TLS
    SECURE_COOKIES=1 \
    BEHIND_PROXY=1

WORKDIR /app

# Dependencies first, so a code change does not re-install them every build.
COPY requirements.txt .
# Ubuntu 24.04 (noble) ships Python 3.12 marked "externally managed" (PEP 668),
# which makes pip REFUSE to install into the system interpreter. This image has
# no virtualenv, so that refusal is a hard build failure. Allowing it is correct
# here: a container IS the isolation a venv would provide, and nothing else
# shares this interpreter.
ENV PIP_BREAK_SYSTEM_PACKAGES=1 \
    PIP_ROOT_USER_ACTION=ignore \
    PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m pip install --no-cache-dir --upgrade pip \
 && python -m pip install --no-cache-dir -r requirements.txt \
 && python -c "import flask, waitress, playwright, PIL, openpyxl, rapidfuzz, apscheduler; print('deps import cleanly')"

COPY . .

# NO `VOLUME` instruction here. Railway rejects it outright:
#   "dockerfile invalid: docker VOLUME is not supported, use Railway Volumes"
# The mount is declared in the platform (Railway: service -> Volumes, mount
# path /app/data), not in the image. Same requirement either way, just stated
# somewhere else: data/ holds the database, every snapshot and every
# screenshot, and the container filesystem is discarded on each deploy. Without
# a volume mounted there, every deploy throws away every page's baseline and
# the next run re-baselines all 4,188 pages and reports nothing for a cycle.
# On a plain Docker host, mount it yourself: -v iradar-data:/app/data

EXPOSE 5000

# Fails the container when the app stops answering, so the platform restarts it
# instead of leaving a dead dashboard up. Reads PORT for the same reason app.py
# does - the platform chooses it, we do not.
HEALTHCHECK --interval=60s --timeout=10s --start-period=120s --retries=3 \
  CMD python -c "import os,sys,urllib.request; p=os.environ.get('PORT','5000'); sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{p}/login',timeout=8).status==200 else 1)"

# app.py serves on waitress and starts the scheduler, the capture runner and
# the housekeeping threads in this same process - it is the whole application,
# not just a web server, so it is the entrypoint.
CMD ["python", "iradar/app.py"]
