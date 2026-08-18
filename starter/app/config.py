"""Configuration for the Industrial Edge Telemetry Gateway (reference solution).

All values are intentionally constants so the app runs fully offline with no
network access at runtime. The simulator is expected to be running and reachable
via HTTP on the port below.
"""
from __future__ import annotations

import os

# Where the simulated machine fleet API lives. Overridable via env for tests.
SIMULATOR_BASE_URL = os.environ.get("SIMULATOR_BASE_URL", "http://127.0.0.1:8777")

# Max time a single device poll may take before it is treated as a timeout.
POLL_TIMEOUT_SECONDS = float(os.environ.get("POLL_TIMEOUT_SECONDS", "5.0"))

# Path to the SQLite database file used for persistent state.
DB_PATH = os.environ.get("GATEWAY_DB_PATH", "gateway.db")

# Threshold for the consecutive-failure COMMUNICATION_LOST counter.
COMMUNICATION_LOST_THRESHOLD = 3

# Boundaries for the restart predicate (§5).
RESTART_PREV_SEQ_MIN = 900      # current_latest_sequence must be >= this
RESTART_NEW_SEQ_MAX = 100       # incoming sequence must be <= this
