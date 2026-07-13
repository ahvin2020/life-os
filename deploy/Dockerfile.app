# Life OS web app — Flask on port 5070. Base image is multi-arch (works on the
# Intel DS423+ and on ARM alike).
FROM python:3.12-slim

# Node 20 + the Claude CLI: the notes "Ask" Q&A shells out to `claude -p` (headless
# auth via CLAUDE_CODE_OAUTH_TOKEN), same as the capture image.
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g @anthropic-ai/claude-code \
    && npm cache clean --force \
    && apt-get purge -y curl gnupg && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Bake the app code INTO the image (the .dockerignore keeps vault/data/.env out).
# The image IS the deploy artifact now: git push → CI builds → ghcr → Watchtower pulls.
# vault/ and data/ are the only things mounted at runtime (user data). See deploy/README.md.
COPY . /app
ENV TZ=Asia/Singapore
EXPOSE 5070
CMD ["python3", "server.py", "--port", "5070", "--db", "/data/app.db"]
