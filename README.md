# 기억 서랍 (Memory Agent)

Slack 메시지, 이메일, 메모를 붙여넣으면 OpenAI로 내용을 구조화하고 임베딩한 뒤
Supabase `pgvector`에 저장하는 개인용 기억 관리 웹앱입니다. 저장된 내용만 근거로
질문에 답하고, 답변에 사용한 원문을 함께 확인할 수 있습니다.

## 주요 기능

- Slack, 이메일, 일반 메모 자동 판별 및 주제 단위 청킹
- 담당자, 프로젝트, 상태, 업무일, 마감일, 카테고리, 태그 자동 추출
- 담당자·프로젝트·상태·기간 필터와 업무일별 타임라인
- 업무, 계정·접속, 자료·강의, 메모 유형별 탭과 계정 전용 상세 표시
- 벡터 의미 검색과 사람 이름 정확 검색을 결합한 하이브리드 검색
- `그중`, `그 사람`, `해당 업무` 같은 후속 질문을 독립 검색문으로 선택적 재작성
- 인물 질문에서 다른 인물·문서의 벡터 검색 결과를 제외해 업무 혼합 방지
- `harness.md` 기반 답변 근거·업무 보고 형식 관리
- 제목, 상태 강조, 불릿과 링크를 지원하는 안전한 제한 Markdown 렌더링
- `오늘`, `어제`, `이번 주`, `지난주`, `이번 달` 기반 날짜 검색
- 동일 본문 SHA-256 기반 원자적 갱신, 선삭제 없는 중복 처리, 만료 기억 정리
- 저장된 본문과 담당자·프로젝트·상태·날짜·유형·태그 수정 및 재임베딩
- 답변 근거 원문 조회
- OpenAI 응답을 실시간으로 표시하는 스트리밍 채팅
- 브라우저 `localStorage` 기반 대화 이력 복원 및 전체 삭제
- 사용자별 `viewer`·`editor`·`admin` 권한, 만료 세션, 로그아웃, 감사 로그

## 처리 구조

```text
붙여넣기
  -> OpenAI 텍스트 분석 및 구조화
  -> OpenAI 임베딩 생성
  -> Supabase PostgreSQL + pgvector 저장

질문
  -> 문맥 의존 후속 질문만 독립 검색문으로 재작성
  -> 이름·날짜 정확 검색 + 벡터 유사도 검색
  -> 태그 리랭킹
  -> OpenAI 스트리밍 답변 생성
  -> 생성되는 답변과 근거 원문 표시
```

Redis는 필수 구성요소가 아닙니다. 현재는 Supabase가 영구 저장과 벡터 검색을
담당합니다. 다중 사용자 응답 캐시, 비동기 수집 작업 큐, 세션 저장이 필요해질 때
Redis를 추가하는 것이 적절합니다.

대화 이력은 현재 브라우저의 `localStorage`에 최근 12개 메시지를 저장합니다.
따라서 새로고침에는 유지되지만 다른 브라우저나 기기와는 공유되지 않습니다.
기기 간 동기화가 필요하면 Supabase 대화 테이블 또는 Redis 기반 서버 세션을 추가해야 합니다.

## 요구 사항

- Python 3.12 권장
- Supabase 프로젝트
- OpenAI API 키
- WSL 또는 Linux/macOS 환경 권장

## Supabase 설정

Supabase SQL Editor에서 설치 상태에 맞는 SQL을 실행합니다.

- 신규 프로젝트: `schema.sql`
- 기존 프로젝트: `migration_security.sql`
- `migration_expiry.sql`은 과거 설치용 유통기한 마이그레이션이며,
  `migration_security.sql`에 해당 변경이 포함되어 있습니다.

보안 마이그레이션은 `memories`와 `audit_logs`에 RLS를 적용하고 `anon`,
`authenticated`, `PUBLIC`의 테이블/RPC 권한을 회수합니다. 백엔드는
`SUPABASE_SERVICE_KEY`를 서버 환경변수로만 사용해야 하며 브라우저에 노출하면 안 됩니다.

## 환경변수

`.env.example`을 참고해 프로젝트 루트에 `.env`를 만듭니다.

```dotenv
OPENAI_API_KEY=your-openai-api-key
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_SERVICE_KEY=your-supabase-service-role-key

# 검색·처리 설정
CHAT_MODEL=gpt-4o-mini
EMBED_MODEL=text-embedding-3-small
SIM_THRESHOLD=0.25
TOP_K=8
CATALOG_CACHE_TTL=30
MAX_CONTEXT_CHARS=12000
MAX_CATALOG_ROWS=5000
MAX_INGEST_CHARS=20000

# 사용자별 인증
APP_ENV=development
APP_SECRET=replace-with-a-random-secret-at-least-16-characters
APP_USERS_JSON='{"admin":{"password":"change-me","role":"admin"},"member":{"password":"change-me-too","role":"editor"}}'
SESSION_TTL_SECONDS=43200
COOKIE_SECURE=false
```

역할별 권한은 다음과 같습니다.

- `viewer`: 기억 조회와 질문
- `editor`: `viewer` 권한 + 기억 저장과 수정
- `admin`: 전체 권한 + 삭제, 만료 정리, 감사 로그 조회

`APP_USERS_JSON`의 `password` 대신 `password_hash`에
`pbkdf2_sha256$반복횟수$salt$hash` 값을 넣을 수 있습니다. 기존 단일 비밀번호
설정인 `APP_PASSWORD`도 호환되며 로그인 사용자명은 `admin`, 권한도 `admin`으로
기록됩니다. 운영 환경에서는 평문 `password`보다 `password_hash` 사용을 권장합니다.

```bash
python scripts/hash_password.py
```

출력된 전체 값을 해당 사용자의 `password_hash`에 넣습니다.

Windows에서 편집한 `.env`를 WSL에서 `source`할 경우 CRLF가 환경변수에 포함될 수
있습니다. 다음 명령으로 줄바꿈을 정리할 수 있습니다.

```bash
sed -i 's/\r$//' .env
```

## 설치 및 실행

`uv`를 사용하는 방법을 권장합니다.

```bash
uv python install 3.12
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements.txt

source .venv/bin/activate
uvicorn main:app --reload --port 8000
```

브라우저에서 `http://localhost:8000`으로 접속합니다.

일반 `venv`와 `pip`를 사용해도 됩니다.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m uvicorn main:app --reload --port 8000
```

### 테스트

개발 의존성을 설치한 뒤 전체 테스트를 실행합니다.

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

### WSL 회사 인증서

회사 TLS 인증서를 WSL 시스템 저장소가 이미 신뢰하는 경우 Python에도 시스템 CA
번들을 지정합니다.

```bash
export SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
```

매번 설정하지 않으려면 `.venv/bin/activate` 마지막에 같은 줄을 추가할 수 있습니다.

```bash
echo 'export SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt' >> .venv/bin/activate
```

## 사용법

### 저장

왼쪽 입력란에 Slack 스레드, 이메일 또는 메모를 붙여넣고 `저장하기`를 누릅니다.
`Ctrl+Enter` 또는 `Cmd+Enter`로도 저장할 수 있습니다.

저장 레코드에는 다음 메타데이터가 포함됩니다.

```json
{
  "person": "조재경",
  "project": "Hynix G4",
  "status": "진행 중",
  "work_date": "2026-08-06",
  "due_date": null,
  "category": "업무",
  "tags": ["ATL", "Langflow", "인증시험"]
}
```

기존 레코드에 새 필드가 없으면 발신자, 태그, 본문, 저장일에서 표시용 값을
자동으로 보완합니다.

### 검색

오른쪽 질문란에서 자연어로 검색합니다.

```text
최윤서 매니저는 무엇을 하고 있어?
지난주 조재경 매니저가 완료한 업무는?
이번 달 ATL 진행 중 업무를 알려줘
```

### 관리

- 담당자, 프로젝트, 상태, 시작일, 종료일로 최근 기록 필터링
- 유형 탭으로 업무와 계정·비밀번호, 자료·강의를 분리해서 조회
- 최근 기록은 10건씩 표시하고 `더 보기`로 추가 조회
- 태그를 눌러 관련 기록만 조회
- `×` 버튼으로 개별 기록 삭제
- 연필 버튼으로 본문과 구조화 메타데이터 수정
- `만료 정리`로 만료된 기록 일괄 삭제
- 관리자 헤더의 `활동 로그`에서 로그인, 질문, 저장, 수정, 삭제 이력 조회

## 검색 설정

- `SIM_THRESHOLD`: 벡터 검색 최소 유사도. 높이면 누락이 늘고 오답이 줄어듭니다.
- `TOP_K`: 답변 생성에 사용할 최대 검색 결과 수입니다.
- `CATALOG_CACHE_TTL`: 키워드·태그 검색용 메모리 카탈로그 캐시 시간(초)입니다.
- `MAX_CATALOG_ROWS`: 정확 검색용 활성 기억 카탈로그의 최대 행 수입니다.
- `MAX_INGEST_CHARS`: 한 번에 붙여넣을 수 있는 최대 문자 수입니다.
- `MAX_CONTEXT_CHARS`: 답변 모델에 전달할 검색 원문의 최대 문자 수입니다.
- `PARSER_SYSTEM`: 저장 시 구조화 규칙입니다.
- `ANSWER_SYSTEM`: 저장된 정보로 답변하는 규칙입니다.
- `harness.md`: 인물·프로젝트·상태별 답변 형식과 사실 근거 규칙입니다.

## 포트 확인과 종료

```bash
ss -ltnp | grep ':8000'
fuser -v 8000/tcp
fuser -k 8000/tcp
```

현재 터미널에서 실행한 Uvicorn은 `Ctrl+C`로 종료합니다.

## 배포

로컬 개발에서는 인증 설정이 비어 있으면 `local/admin`으로 동작합니다. Railway와
Render 또는 `APP_ENV=production` 환경에서는 `APP_USERS_JSON`이나 `APP_PASSWORD`가
없으면 서버가 시작되지 않습니다. 운영 환경에서는 반드시 사용자별 계정과 16자
이상의 `APP_SECRET`을 설정합니다.

```bash
python -c "import secrets; print(secrets.token_hex(24))"
```

Render, Railway 등 Docker를 지원하는 서비스에서는 저장소를 연결하고 다음 값을
환경변수로 등록합니다.

- `OPENAI_API_KEY`
- `SUPABASE_URL`
- `SUPABASE_SERVICE_KEY`
- `APP_ENV=production`
- `APP_USERS_JSON`
- `APP_SECRET`

배포 후 `/healthz`가 `{"status":"ok"}`를 반환하는지 확인합니다.

## 보안 주의사항

- `.env`는 Git에 커밋하지 않습니다.
- API 키가 로그나 대화에 노출되면 즉시 폐기하고 새로 발급합니다.
- 저장 내용과 질문은 처리 과정에서 OpenAI와 Supabase로 전송됩니다.
- API 키나 비밀번호를 기억으로 저장할 수 있지만 외부 API로 전송된다는 점을
  이해한 경우에만 사용해야 합니다.
- Supabase 테이블과 검색 RPC는 `service_role`만 접근할 수 있도록 제한되어 있습니다.
  사용자 인증·권한 검사는 FastAPI에서 수행하고 모든 변경은 `audit_logs`에 기록합니다.
- 응답에는 CSP, 프레임 차단, MIME 스니핑 차단 등 기본 브라우저 보안 헤더가 적용됩니다.
