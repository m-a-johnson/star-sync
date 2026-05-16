FROM python:3.12-alpine

WORKDIR /app

# Install dependencies
RUN pip install --no-cache-dir \
    requests==2.32.3 \
    pyyaml==6.0.2 \
    mutagen==1.47.0

COPY sync.py .

# -u = unbuffered stdout/stderr so logs appear immediately in docker logs
CMD ["python", "-u", "sync.py"]