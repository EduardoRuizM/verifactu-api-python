FROM python:3.13-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    default-libmysqlclient-dev gcc && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./

RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8023

CMD ["python", "run.py"]
