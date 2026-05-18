FROM python:3.12-slim

# Production deps. Keep this set minimal — see AGENTS.md ("Daemon code").
RUN pip install --no-cache-dir \
    requests \
    docker \
    python-dotenv

# Dev-only deps (intentionally NOT installed in this image):
#   - pytest  — runs agent-team/daemon/tests/. The test files are written so
#               `python3 agent-team/daemon/tests/test_regex.py` works without
#               pytest (vanilla assertions), but `pytest` is the nicer runner
#               locally. Install it on the host if you want it
#               (`pip install --user pytest`).

WORKDIR /app
COPY daemon.py .

CMD ["python", "-u", "daemon.py"]
