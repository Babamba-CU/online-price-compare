FROM python:3.12-slim

WORKDIR /app

# 시스템 tzdata + TZ=Asia/Seoul → date.today()/스케줄러가 KST 기준으로 동작
# (pip tzdata 아닌 OS tzdata 가 있어야 C 라이브러리 localtime 이 KST 로 계산됨)
ENV TZ=Asia/Seoul
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata \
 && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
 && echo $TZ > /etc/timezone \
 && rm -rf /var/lib/apt/lists/*

# 의존성만 먼저 설치 (캐시 활용)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 앱 소스 + 정적 자원
COPY . .

# 내장 스케줄러가 *_data.js / *.db 를 in-place 갱신할 수 있도록 /app 소유권을 UID 1000 으로
# (COPY 는 root 소유로 복사되므로 chown 없으면 USER 1000 이 덮어쓸 수 없음)
RUN chown -R 1000:1000 /app

EXPOSE 8080

# Polaris Colab: non-root (UID 1000) 강제
USER 1000

CMD ["python", "app.py"]
