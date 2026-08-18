# Odyssey task bundle: Industrial Edge Telemetry Gateway
# Builds an image whose runtime is fully offline: the simulator and verifier are
# bundled into the image and no network access is required while the task runs.
# Build from the bundle root: docker build -f environment/Dockerfile .
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /task

# Dependencies are installed at image-build time. The finished task runtime
# does not require network access.
COPY environment/requirements.txt /task/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy the whole bundle (simulator, gateway reference + starter app, tests, verifier).
COPY . /task

# The candidate gateway is expected at /app (mounted/extracted by the grader).
# tests/test.sh falls back to /task/solution/app when /app is absent, so the
# bundle is self-validating out of the box.
EXPOSE 8111

# Sealed evaluation entrypoint.
CMD ["bash", "tests/test.sh"]
