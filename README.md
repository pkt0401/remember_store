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
- 기억 공간별 SHA-256 중복 처리와 만료 기억 정리
- 저장된 본문과 담당자·프로젝트·상태·날짜·유형·태그 수정 및 재임베딩
- 답변 근거 원문 조회
- OpenAI 응답을 실시간으로 표시하는 스트리밍 채팅
- 브라우저 `localStorage` 기반 대화 이력 복원 및 전체 삭제
- 사용자별 `viewer`·`editor`·`admin` 권한, 만료 세션, 로그아웃, 감사 로그
- 모든 계정이 보는 공유 기억과 계정 UUID별 개인기억 분리
- OpenAI API 키 형태의 입력 차단과 기존 키 포함 기억 격리

## 처리 구조

```text
붙여넣기
  -> OpenAI 텍스트 분석 및 구조화
  -> OpenAI 임베딩 생성
  -> Supabase PostgreSQL + pgvector 저장

질문
  -> 문맥 의존 후속 질문만 독립 검색문으로 재작성
  -> 공유 기억 + 현재 계정의 개인기억에서 이름·날짜·벡터 검색
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
- 기존 프로젝트: `migration_security.sql` 실행 후 `migration_memory_scopes.sql`
- `migration_expiry.sql`은 과거 설치용 유통기한 마이그레이션이며,
  `migration_security.sql`에 해당 변경이 포함되어 있습니다.

`migration_memory_scopes.sql`은 기존 기억을 공유 기억으로 전환하고, 실제 OpenAI
API 키 형태가 포함된 행은 일반 기억에서 제거합니다. 키 원문·본문 해시·임베딩은
영구 폐기하며, `quarantined_memories`에는 키를 마스킹한 감사용 사본만 남습니다.
실행 후 앱을 재시작해 기존 메모리 캐시를 비웁니다. 격리된 키가 실제 사용 중인
키였다면 OpenAI 대시보드에서 폐기하고 새 키로 교체하세요.

Supabase Auth에서 이메일/비밀번호 로그인을 활성화하고 사용자를 생성합니다. Auth의
불변 `user.id` UUID가 개인기억 소유권으로 사용됩니다. 신규 사용자는 기본 `editor`로
동작하며, 읽기 전용 또는 관리자 계정은 Auth 사용자의 `app_metadata.app_role`을
각각 `viewer` 또는 `admin`으로 설정합니다. 기존 `APP_USERS_JSON`의 계정과 비밀번호
해시는 Supabase Auth로 자동 이전되지 않으므로 사용자를 새로 만들거나 초대해야 합니다.

## 환경변수

`.env.example`을 참고해 프로젝트 루트에 `.env`를 만듭니다.

```dotenv
OPENAI_API_KEY=your-openai-api-key
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_PUBLISHABLE_KEY=your-supabase-publishable-key
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

# Supabase Auth 세션
APP_ENV=development
SESSION_TTL_SECONDS=43200
COOKIE_SECURE=false
```

역할별 권한은 다음과 같습니다.

- `viewer`: 기억 조회와 질문
- `editor`: `viewer` 권한 + 기억 저장, 자신이 소유·생성한 기억 수정·삭제
- `admin`: 공유 기억 전체 관리 + 만료 정리, 감사 로그 조회

모든 질문은 공유 기억과 현재 로그인 UUID의 개인기억을 함께 검색합니다. 다른 계정의
개인기억은 목록, 필터, 태그, 벡터 검색 및 답변 원문에 포함되지 않습니다.

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

로컬과 운영 환경 모두 Supabase Auth 로그인이 필요합니다. 사용자 데이터 요청은
publishable key와 요청별 사용자 JWT로 실행해 RLS를 적용하고, service key는 감사
로그와 명시적인 관리자 작업에만 사용합니다.

Render, Railway 등 Docker를 지원하는 서비스에서는 저장소를 연결하고 다음 값을
환경변수로 등록합니다.

- `OPENAI_API_KEY`
- `SUPABASE_URL`
- `SUPABASE_PUBLISHABLE_KEY`
- `SUPABASE_SERVICE_KEY`
- `APP_ENV=production`

배포 후 `/healthz`가 `{"status":"ok"}`를 반환하는지 확인합니다.

## 보안 주의사항

- `.env`는 Git에 커밋하지 않습니다.
- `OPENAI_API_KEY`와 RLS를 우회하는 `SUPABASE_SERVICE_KEY`는 모두 서버 전용
  비밀입니다. 브라우저·공유 기억·Git에 넣지 않습니다. 클라이언트에 공개할 수 있는
  값은 `SUPABASE_PUBLISHABLE_KEY`뿐입니다.
- 비밀 키가 로그나 대화에 노출되면 즉시 폐기하고 새로 발급합니다.
- 저장 내용과 질문은 처리 과정에서 OpenAI와 Supabase로 전송됩니다.
- OpenAI API 키 형태는 기억 저장 전에 차단됩니다. 다른 비밀번호도 개인기억으로만
  저장하고, 저장 내용이 처리 과정에서 OpenAI와 Supabase로 전송된다는 점을 확인하세요.
- 기억 테이블과 검색 RPC는 사용자 JWT의 `auth.uid()`를 이용한 RLS와 FastAPI의
  범위 필터를 함께 적용합니다. `SUPABASE_SERVICE_KEY`는 브라우저에 노출하지 않습니다.
- 응답에는 CSP, 프레임 차단, MIME 스니핑 차단 등 기본 브라우저 보안 헤더가 적용됩니다.
