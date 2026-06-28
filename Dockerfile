FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir "starlette>=0.37,<1" "uvicorn>=0.30,<1" "httpx>=0.27,<1"
COPY app.py .
EXPOSE 8000
CMD ["python", "app.py"]
