# Build the front end, then serve it and the API from one process.
#
# One image, one URL: the browser fetches /api from the same origin it was
# served from, so there is no CORS surface and no second thing to deploy.

FROM node:20-slim AS web
WORKDIR /app
COPY app/package.json app/package-lock.json* ./
RUN npm install --no-audit --no-fund
COPY app/ ./
RUN npm run build

FROM python:3.12-slim
WORKDIR /srv

# psycopg needs no build toolchain with the binary wheel, which is what
# requirements.txt asks for.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt flask gunicorn

COPY racelab/ ./racelab/
COPY scenario/ ./scenario/
COPY scripts/ ./scripts/
COPY deploy/ ./deploy/
COPY bindings/ ./bindings/
COPY --from=web /app/dist ./app/dist

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

# gthread, one worker: the single-run lock that protects the cluster's
# connection budget is process-local, so a second worker would silently double
# the concurrent races. Threads (not workers) are what let /api/state stay
# answerable while an SSE stream is held open.
CMD ["gunicorn", "-k", "gthread", "-w", "1", "--threads", "24", \
     "-t", "300", "--graceful-timeout", "30", \
     "-b", "0.0.0.0:8080", "racelab.server:app"]
