# Dockerfile - Containerizing the PyCaret FastAPI app
FROM python:3.11-slim

WORKDIR /app

# 1) Install OS-level dependencies for LightGBM and health checks
RUN apt-get update \
 && apt-get install -y --no-install-recommends libgomp1 curl \
 && rm -rf /var/lib/apt/lists/*

# 2) Copy & install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 3) Copy FastAPI app and modules
COPY app.py .
COPY fsstate.py .
COPY training/ training/
COPY shared/ shared/
COPY scheduling/ scheduling/
COPY profiling/ profiling/

# When using containerization these should be located in a shared volume
# COPY model.pkl .
# COPY status.json .
# COPY training.csv .

RUN useradd --create-home appuser \
 && mkdir -p /shared \
 && chown appuser:appuser /shared
USER appuser

# 4) Expose port and run via main() entry point
EXPOSE 8000
CMD ["python", "app.py"]
