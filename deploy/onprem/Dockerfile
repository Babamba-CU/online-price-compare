# 사내망 서빙 이미지 — GitLab 화면 코드 + 사내 PostgreSQL 연결
# 빌드 컨텍스트는 repo 루트:  docker build -f deploy/onprem/Dockerfile -t price-onprem .
FROM python:3.12-slim

WORKDIR /app

# tzdata: date.today()/스케줄러 KST 보장 (사내 폐쇄망에서 pip tzdata 아닌 OS tzdata)
ENV TZ=Asia/Seoul
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata \
 && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone \
 && rm -rf /var/lib/apt/lists/*

COPY deploy/onprem/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 서빙에 필요한 것만: 화면 + 폰트/라이브러리 + PG 빌더 (수집 스크립트 미포함)
COPY index.html app.py build_from_pg.py ./
COPY vendor ./vendor

RUN chown -R 1000:1000 /app
EXPOSE 8080
USER 1000

# 사내망 모드: 수집 대신 PostgreSQL 에서 화면 데이터 빌드. DB 접속은 런타임 환경변수.
ENV DATA_SOURCE=postgres \
    REFRESH_HOUR=5 \
    DB_PORT=5432
# DB_HOST / DB_USER / DB_PASSWORD / DB_NAME 은 docker run -e 또는 --env-file 로 주입

CMD ["python", "app.py"]
