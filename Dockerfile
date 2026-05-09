FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV DATA_DIR=/data

ENV PORT=8080
ENV BIND_HOST=0.0.0.0
EXPOSE 8080

CMD ["python", "-u", "entrypoint.py"]
