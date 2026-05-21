# Polaris Colab 배포 규칙

이 앱은 Polaris Colab 플랫폼에서 컨테이너로 호스팅됩니다.

## 핵심 규칙 (MUST)

1. 서버는 반드시 `0.0.0.0:8080`에서 리슨. 다른 포트 사용 시 배포 실패.
2. `GET /health` 엔드포인트 필수. 없으면 컨테이너가 반복 재시작됨.
3. DB 접속 정보는 환경변수로 읽기. 하드코딩 시 배포 환경에서 연결 불가.

## 포트 8080 설정

```python
# Python Flask
app.run(host="0.0.0.0", port=8080)

# Python FastAPI
uvicorn.run(app, host="0.0.0.0", port=8080)
```

```js
// Express
app.listen(8080, "0.0.0.0", () => {});
```

## 헬스체크 엔드포인트

```python
# Flask
@app.route("/health")
def health():
    return "ok", 200

# FastAPI
@app.get("/health")
def health():
    return {"status": "ok"}
```

```js
// Express
app.get("/health", (req, res) => res.send("ok"));
```

## DB 연결 (환경변수 MUST)

환경변수: `DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASSWORD`, `DB_NAME`
MariaDB 포트 3306, PostgreSQL 포트 5432

```python
import os

db_host     = os.getenv("DB_HOST")
db_port     = os.getenv("DB_PORT", "3306")
db_user     = os.getenv("DB_USER")
db_password = os.getenv("DB_PASSWORD")
db_name     = os.getenv("DB_NAME")
```

```js
const dbHost     = process.env.DB_HOST;
const dbPort     = process.env.DB_PORT || "3306";
const dbUser     = process.env.DB_USER;
const dbPassword = process.env.DB_PASSWORD;
const dbName     = process.env.DB_NAME;
```

## NEVER DO

```python
app.run()                          # 기본 포트 5000 사용 -> 배포 실패
app.run(host="localhost")          # 외부 접속 불가
conn = pymysql.connect(host="10.20.30.40", password="secret")  # 하드코딩
open("/data/file.csv", "w")        # 재시작 시 소멸, /tmp만 사용
```

## Dockerfile (MUST — 직접 작성 권장)

자동 생성 Dockerfile은 프로젝트 구조를 잘못 인식하여 배포 실패를 유발할 수 있습니다. 반드시 직접 작성하세요.

### Python 프로젝트

```dockerfile
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8080

USER 1000

CMD ["python", "app.py"]
```

### Node.js 프로젝트

```dockerfile
FROM node:20-slim

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci --only=production

COPY . .

EXPOSE 8080

USER 1000

CMD ["node", "index.js"]
```

## .dockerignore (MUST)

로컬 개발 환경 파일이 이미지에 포함되면 빌드 실패 또는 용량 초과가 발생합니다.

```
.venv
node_modules
.git
__pycache__
*.pyc
*.md
.env
```

## 기타 제약

- 앱은 non-root(UID 1000)로 실행됨. root 권한 필요한 작업 불가.
- 파일 쓰기는 `/tmp`만 가능. 영구 저장은 DB 사용.
- 의존성 파일(`requirements.txt` 또는 `package.json`) 필수. 없으면 빌드 실패.
- Node.js 프로젝트는 `package-lock.json`을 반드시 repo에 포함. 없으면 의존성 버전 충돌로 빌드 실패할 수 있음.
- SPA(React/Vite/Svelte)는 `base: '/'`로 설정.

## 자기검증 체크리스트

코드 작성 후 확인:

- [ ] 서버가 `0.0.0.0:8080`에서 리슨하는가?
- [ ] `GET /health`가 200을 반환하는가?
- [ ] DB 정보를 환경변수에서 읽는가?
- [ ] `localhost`로 바인딩하고 있지 않은가?
- [ ] `requirements.txt` 또는 `package.json`이 루트에 있는가?
- [ ] Node.js 프로젝트라면 `package-lock.json`이 repo에 포함되어 있는가?
- [ ] `Dockerfile`이 루트에 있고, `EXPOSE 8080`과 `USER 1000`이 포함되어 있는가?
- [ ] `.dockerignore`에 `.venv`, `node_modules`, `.git`이 포함되어 있는가?
