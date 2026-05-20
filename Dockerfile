FROM python:3.12-slim

WORKDIR /app

# 의존성만 먼저 설치 (캐시 활용)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 앱 소스 + 정적 자원
COPY . .

EXPOSE 8080

# Polaris Colab: non-root (UID 1000) 강제
USER 1000

CMD ["python", "app.py"]
