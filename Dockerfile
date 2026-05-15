FROM python:3.12-alpine

WORKDIR /app

# Install dependencies — only 'requests' needed, no heavy frameworks
RUN pip install --no-cache-dir requests==2.32.3

COPY sync.py .

# -u = unbuffered stdout/stderr so logs appear immediately in docker logs
CMD ["python", "-u", "sync.py"]
