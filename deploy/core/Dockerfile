FROM python:3.12-slim
WORKDIR /app
COPY . /app
RUN pip install --no-cache-dir .
ENV DIGEST_DB_PATH=/data/digest.db DIGEST_DATA_DIR=/data
EXPOSE 8787
CMD ["digest-core", "serve", "--host", "0.0.0.0", "--port", "8787"]
