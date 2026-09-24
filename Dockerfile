FROM python:3.13-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg tini && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml ./
COPY requirements.lock ./
COPY app ./app
RUN pip install --no-cache-dir -r requirements.lock && pip install --no-cache-dir --no-deps .
ENV AF_DATA=/config PYTHONUNBUFFERED=1
EXPOSE 8095
ENTRYPOINT ["/usr/bin/tini", "-g", "--"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8095"]
