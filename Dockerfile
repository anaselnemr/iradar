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
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# data/ holds the database, every snapshot and every screenshot. It MUST be a
# mounted volume: the container filesystem is thrown away on each deploy, and
# with it every page's baseline - the next run would re-baseline all 4,188
# pages and report nothing for a cycle.
VOLUME ["/app/data"]

EXPOSE 5000

# Fails the container when the app stops answering, so the platform restarts it
# instead of leaving a dead dashboard up.
HEALTHCHECK --interval=60s --timeout=10s --start-period=90s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:5000/login',timeout=8).status==200 else 1)"

# app.py serves on waitress and starts the scheduler, the capture runner and
# the housekeeping threads in this same process - it is the whole application,
# not just a web server, so it is the entrypoint.
CMD ["python", "app.py"]
