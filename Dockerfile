FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PORT=8080
ENV DATABASE_PATH=data/orbis_finance.db

EXPOSE 8080

CMD ["sh", "-c", "python -m src.app serve-form --host 0.0.0.0 --port ${PORT}"]
