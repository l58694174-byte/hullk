FROM python:3.11-slim

WORKDIR /app

RUN pip install --upgrade pip

RUN pip install --no-cache-dir "python-telegram-bot==21.5"

COPY bot.py .

CMD ["python", "-u", "bot.py"]
