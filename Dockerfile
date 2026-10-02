# For hosts that build from a Dockerfile (Koyeb, Fly.io, Hugging Face Spaces, your own server)
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 HOSTED=1 PORT=8000
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY seed_data ./seed_data
EXPOSE 8000
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
