FROM python:3.9-slim

WORKDIR /app

# Install dependencies directly
RUN pip install --no-cache-dir mysql-connector-python pathlib

# Create directory for exports
RUN mkdir -p /app/exported_prs

# Default directory for coordination
RUN mkdir -p /coordination

# Default command
CMD ["python", "/app/pr_export.py"]