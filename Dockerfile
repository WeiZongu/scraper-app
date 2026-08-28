# Playwright公式イメージ: Chromium等のブラウザ本体・依存ライブラリが最初から入っている
FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Render/Railway等はコンテナに $PORT を渡してくる。main.py側で os.environ["PORT"] を読む。
EXPOSE 8550

CMD ["python", "main.py"]
