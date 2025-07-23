# Base image - Build stage
FROM python:3.11-slim AS builder

# work directory
WORKDIR /app

# pip update
RUN pip install --upgrade pip

# python library install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt


COPY . .

#logs folder
RUN mkdir -p logs

#run bot
CMD ["python", "choi_bot.py"]
