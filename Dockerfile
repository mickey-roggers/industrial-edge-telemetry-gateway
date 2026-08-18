# Odyssey task bundle: Industrial Edge Telemetry Gateway
# Builds an offline image that runs the sealed verifier (tests/test.sh) which
# starts the bundled simulator + the candidate gateway and scores 5 components.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /task

# Install dependencies first (cached layer).
COPY requirements.txt /task/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy the whole bundle (simulator, gateway reference/starter, tests, verifier).
COPY . /task

# The candidate's gateway is expected at /app (mounted/extracted by the grader).
# tests/test.sh falls back to /task/solution/app when /app is absent, so the
# bundle is self-validating out of the box.
EXPOSE 8111

# Sealed evaluation entrypoint.
CMD ["bash", "tests/test.sh"]
