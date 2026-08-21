FROM --platform=linux/amd64 python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    HYSPLIT_HOME=/opt/hysplit \
    LAKETRAJ_DATA_DIR=/app/data \
    LAKETRAJ_RUNTIME_DIR=/var/data/runtime \
    LAKETRAJ_RESULTS_DIR=/var/data/results

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    libgfortran5 \
    libgomp1 \
    libquadmath0 \
    libstdc++6 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /app/requirements.txt

RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r /app/requirements.txt

COPY app.py /app/app.py
COPY laketraj /app/laketraj
COPY data /app/data

RUN mkdir -p \
    /var/data/runtime/meteorology/gdas1 \
    /var/data/runtime/meteorology/gfs0p25 \
    /var/data/runtime/hysplit_runs \
    /var/data/runtime/results \
    /var/data/results

EXPOSE 10000

CMD ["sh", "-c", "solara run app.py --host=0.0.0.0 --port=${PORT:-8765} --production"]