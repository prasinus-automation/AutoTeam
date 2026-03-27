FROM python:3.12-slim

RUN pip install --no-cache-dir \
    requests \
    docker \
    python-dotenv

WORKDIR /app
COPY daemon.py .

CMD ["python", "-u", "daemon.py"]
