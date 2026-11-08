FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# git stores the configurations; it is the only system package needed
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install ".[ssh]" \
    && useradd --system --no-create-home --uid 10001 cfgtrack \
    && install -d -o cfgtrack /data

# Everything the tool writes (the Git repository, the state file, the key for the password tags) lives in /data, a
# volume; the configuration file is /data/cfgtrack.toml. It makes outgoing connections only, so it runs as an
# unprivileged user with a read-only root filesystem and no capabilities. Passwords come from environment variables.
USER cfgtrack
WORKDIR /data
VOLUME ["/data"]
ENTRYPOINT ["cfgtrack"]
CMD ["backup"]
