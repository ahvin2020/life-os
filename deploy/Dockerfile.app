# Life OS web app — Flask on port 5060. Base image is multi-arch (works on the
# Intel DS423+ and on ARM alike).
FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Code is bind-mounted read-only at runtime (the Synology Drive synced folder);
# nothing app-specific is baked into the image, so a code edit + container restart
# is the whole deploy loop. See deploy/README.md.
ENV TZ=Asia/Singapore
EXPOSE 5060
CMD ["python3", "server.py", "--port", "5060", "--db", "/data/app.db"]
