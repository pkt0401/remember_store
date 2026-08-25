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
- 저장 전 의미 유사 기억 확인과 사용자 확인 후 저장
- 저장된 본문과 담당자·프로젝트·상태·날짜·유형·태그 수정 및 재임베딩
- 답변 근거 원문 조회
- OpenAI 응답을 실시간으로 표시하는 스트리밍 채팅
- 브라우저 `localStorage` 기반 대화 이력 복원 및 전체 삭제
- 사용자별 `viewer`·`editor`·`admin` 권한, 만료 세션, 로그아웃, 감사 로그
- 모든 계정에 초기 AI 사용권 10회 지급 및 관리자 전용 10회 충전
- 아이디 기반 로그인과 앱 내 이메일/비밀번호 회원가입
- 일반 가입에서 예약된 `admin` 관리자 아이디와 안전한 초기 관리자 생성 도구
- 작성자 포함 서로 다른 2명의 동의 후 공개되는 모두의 기억과 2인 동의 기반 공유 기억 삭제
- 계정 UUID별 개인기억 분리와 소유자의 즉시 수정·삭제
- OpenAI API 키 형태의 입력 차단과 기존 키 포함 기억 격리

## 처리 구조

```text
붙여넣기
  -> 선택한 기억 범위에서 유사 기억 검색
  -> 유사 후보가 있으면 사용자 확인 후 계속
  -> OpenAI 텍스트 분석 및 구조화
  -> OpenAI 임베딩 생성
  -> 개인기억: Supabase PostgreSQL + pgvector에 즉시 저장
  -> 모두의 기억: 작성자 1차 동의 -> 다른 사용자 2차 동의 -> 전체 공개
  -> 관리자 모두의 기억: 승인 없이 즉시 공개

삭제
  -> 개인기억: 소유자가 즉시 삭제
  -> 모두의 기억: 요청자 1차 동의 -> 다른 사용자 2차 동의 -> 실제 기억 삭제
  -> 관리자 모두의 기억: 승인 없이 즉시 삭제

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

대화 이력은 현재 브라우저의 `localStorage`에 인증 사용자 UUID별로 분리해 최근
12개 질문·답변만 저장합니다. 인증 전에 이력을 읽지 않으며, 로그아웃하거나 같은
브라우저의 다른 탭에서 계정이 바뀌면 기존 탭의 대화를 지우고 세션을 다시 확인합니다.
검색 근거의 전체 원문은 저장하지 않습니다. 따라서 질문·답변은 같은 계정의 새로고침에
유지되지만 다른 계정·브라우저·기기와는 공유되지 않습니다.
기기 간 동기화가 필요하면 Supabase 대화 테이블 또는 Redis 기반 서버 세션을 추가해야 합니다.

## 요구 사항

- Python 3.12 권장
- Supabase 프로젝트
- OpenAI API 키
- WSL 또는 Linux/macOS 환경 권장

## Supabase 설정

Supabase SQL Editor에서 설치 상태에 맞는 SQL을 실행합니다.

- 신규 프로젝트: `schema.sql`
- 기존 프로젝트: `migration_security.sql`, `migration_memory_scopes.sql`,
  `migration_auth_accounts.sql`, `migration_ai_usage_credits.sql`,
  `migration_shared_memory_approvals.sql`,
  `migration_shared_memory_deletion_approvals.sql` 순서로 실행
- `계정은 Memory Agent 회원가입 API를 통해 생성해야 합니다.` 오류가 발생하는
  기존 프로젝트: `migration_remove_legacy_signup_guard.sql`을 먼저 실행
- `migration_expiry.sql`은 과거 설치용 유통기한 마이그레이션이며,
  `migration_security.sql`에 해당 변경이 포함되어 있습니다.

`migration_memory_scopes.sql`은 기존 기억을 공유 기억으로 전환하고, 실제 OpenAI
API 키 형태가 포함된 행은 일반 기억에서 제거합니다. 키 원문·본문 해시·임베딩은
영구 폐기하며, `quarantined_memories`에는 키를 마스킹한 감사용 사본만 남습니다.
실행 후 앱을 재시작해 기존 메모리 캐시를 비웁니다. 격리된 키가 실제 사용 중인
키였다면 OpenAI 대시보드에서 폐기하고 새 키로 교체하세요.

`migration_auth_accounts.sql`은 로그인 아이디를 Supabase Auth의 불변 `user.id` UUID와
연결하는 비공개 `account_profiles` 테이블과 가입 트리거를 만듭니다. 기존 Auth 사용자도
충돌하지 않는 아이디로 backfill합니다. 이메일은 아이디를 실제 Supabase 로그인 주소로
변환할 때만 서버에서 사용하며, 다른 사용자에게 공개되지 않습니다. 과거 설치본의
`require_managed_auth_signup()` 가드는 정상적인 GoTrue 회원가입도 차단하므로
마이그레이션에서 함수 본문을 통과 동작으로 중립화합니다. 이 방식은 `auth.users`
소유권과 다른 Auth 트리거를 변경하지 않습니다.

`migration_ai_usage_credits.sql`은 기존·신규 계정의 AI 사용권을 처음 10회로
설정합니다. 재실행해도 이미 사용하거나 충전한 잔액은 덮어쓰지 않습니다. 차감과
충전은 PostgreSQL 함수의 조건부 업데이트로 처리해 동시 요청에서도 음수가 되지
않으며, 함수 실행 권한은 서버의 `service_role`에만 부여합니다.

`migration_shared_memory_approvals.sql`은 모두의 기억 제안과 사용자별 동의를
저장합니다. 작성자의 동의를 자동으로 첫 번째 동의로 기록하고, 다른 사용자의 두 번째
동의가 들어오면 하나의 트랜잭션에서 제안에 포함된 기억을 공개합니다. 승인 대기
내용은 일반 기억 목록과 AI 검색에서 제외되며, 기존 공유 기억은 공개 상태로
유지됩니다.

`migration_shared_memory_deletion_approvals.sql`은 공개된 모두의 기억에 대한 삭제
요청과 사용자별 동의를 저장합니다. 요청자의 동의를 첫 번째 동의로 자동 기록하고,
다른 사용자의 두 번째 동의가 들어오면 하나의 트랜잭션에서 실제 기억을 삭제합니다.
승인 대기 중인 기억은 계속 목록·검색·AI 답변에 포함됩니다. 삭제 뒤에도 요청 당시의
본문·출처 스냅샷과 승인 기록은 감사 이력으로 남으며, 관리자는 승인 없이 즉시
삭제할 수 있습니다.

Supabase Auth에서 이메일/비밀번호 로그인을 활성화합니다. Confirm email이 켜져 있으면
가입자는 확인 메일의 링크를 누른 뒤 로그인하고, 꺼져 있으면 가입 직후 자동 로그인됩니다.
확인 메일을 사용할 때는 Supabase Auth의 URL Configuration에서 Site URL을 실제 앱
주소로 설정합니다.
기존 `APP_USERS_JSON`의 계정과 비밀번호 해시는 자동 이전되지 않습니다.

### 초기 관리자 만들기

SQL 마이그레이션을 실행한 뒤 다음 명령으로 예약 아이디 `admin`을 생성합니다.

```bash
python scripts/bootstrap_admin.py --email admin@example.com
```

관리자 비밀번호는 8~128자로 터미널에서 두 번 숨김 입력하며 파일이나 환경변수에 저장하지 않습니다.
같은 명령을 다시 실행하면 기존 `admin` 계정의 이메일·비밀번호·관리자 역할을 갱신합니다.
입력한 이메일이 정확히 하나의 기존 Auth 계정에 속하면 그 UUID와 기억 소유권을 유지한
채 `admin`으로 승격하며, 이메일이 모호하거나 다른 관리자와 충돌하면 변경 없이 중단합니다.
일반 회원가입에서는 `admin` 아이디를 애플리케이션과 데이터베이스가 모두 거부합니다.

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
SIMILAR_MEMORY_THRESHOLD=0.82
TOP_K=8
CATALOG_CACHE_TTL=30
MAX_CONTEXT_CHARS=12000
MAX_CATALOG_ROWS=5000
MAX_INGEST_CHARS=20000

# Supabase Auth 세션
APP_ENV=development
SESSION_TTL_SECONDS=43200
COOKIE_SECURE=false
SIGNUP_ENABLED=true
```

`SIGNUP_ENABLED`를 생략하면 개발 환경에서는 회원가입이 열리고, Vercel·Railway·
Render 또는 `APP_ENV=production`인 운영 환경에서는 닫힙니다. 운영 회원가입을
의도적으로 열 때만 `SIGNUP_ENABLED=true`를 설정합니다.

역할별 권한은 다음과 같습니다.

- `viewer`: 기억 조회와 질문, 모두의 기억 공개·삭제 제안 동의
- `editor`: `viewer` 권한 + 개인기억 저장·수정·즉시 삭제, 모두의 기억 공개·삭제 제안
- `admin`: 모두의 기억 즉시 공개·수정·삭제 + 만료 정리, 승인, 감사 로그·보안 구조 조회, 사용자 AI 사용권 충전

모든 질문은 공유 기억과 현재 로그인 UUID의 개인기억을 함께 검색합니다. 다른 계정의
개인기억은 목록, 필터, 태그, 벡터 검색 및 답변 원문에 포함되지 않습니다.

관리자는 브라우저에서 `/auth-architecture`를 열어 회원가입, JWT, UUID, RLS와
관리자 생성 흐름을 기술 시각화로 확인할 수 있습니다. 일반 계정은 접근할 수 없습니다.

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

### 회원가입과 로그인

로그인 화면의 `회원가입` 탭에서 아이디, 이메일, 비밀번호를 입력합니다. 아이디는
영문 소문자 또는 숫자로 시작하는 3~32자의 영문 소문자·숫자·점·밑줄·하이픈을
사용합니다. 로그인할 때는 가입 시 정한 아이디만 사용합니다.

회원가입으로 만들어진 계정은 기본 `editor`이며, 권한은 사용자가 수정할 수 없는
Supabase `app_metadata.app_role`로 판정합니다. 아이디는 편의를 위한 이름이고 기억
소유권은 변경되지 않는 Auth UUID를 기준으로 합니다.

### AI 사용 횟수

각 계정은 처음 10회의 AI 사용권을 받습니다. 질문, 새 기억 저장, 기억 본문
수정은 각각 사용자 작업 1회로 계산합니다. 한 작업 안에서 분석과 임베딩처럼 여러
OpenAI 요청이 실행되더라도 잔액은 한 번만 차감합니다. 로그인, 기억 조회와 삭제는
차감하지 않습니다. 서버가 처리 실패나 스트림 중단을 감지하면 예약한 횟수를
자동으로 돌려줍니다.

관리자는 헤더의 `사용자 관리`에서 계정별 잔액을 확인하고 `+10회`씩 충전할 수
있습니다.

### 저장

왼쪽 입력란에 Slack 스레드, 이메일 또는 메모를 붙여넣고 `저장하기`를 누릅니다.
`Ctrl+Enter` 또는 `Cmd+Enter`로도 저장할 수 있습니다.

- `개인기억`: 현재 계정에 즉시 저장되며 소유자만 조회·수정·삭제합니다.
- `모두의 기억`: 작성자의 동의가 자동 기록되고, 다른 계정 한 명이 동의하면 모든
  로그인 사용자에게 공개됩니다. 승인 전에는 일반 목록·검색·AI 답변에 나타나지 않습니다.
- `admin`: 모두의 기억을 승인 대기 없이 즉시 공개하고, 공개된 내용을 관리할 수 있습니다.

공개된 모두의 기억은 `editor`가 삭제를 요청하면 요청자 동의를 포함해 1/2 상태가
됩니다. 다른 계정 한 명이 동의해야 실제로 삭제되며, 같은 사용자가 반복해서 눌러도
동의 수는 늘지 않습니다. `viewer`는 삭제 요청을 새로 만들 수 없지만 기존 요청에는
동의할 수 있습니다. 개인기억은 소유자가 즉시 삭제하고, 관리자는 모두의 기억을
즉시 삭제할 수 있습니다.

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
- 개인기억의 `×` 버튼으로 즉시 삭제하고, 모두의 기억의 `삭제 요청`으로 2인 승인 시작
- 연필 버튼으로 본문과 구조화 메타데이터 수정
- `모두의 기억 승인`에서 대기 중인 제안의 작성자·내용·동의 수를 확인하고 동의
- `모두의 기억 삭제 승인`에서 삭제 요청의 내용·동의 수를 확인하고 두 번째 동의
- `만료 정리`로 만료된 기록 일괄 삭제
- 관리자 헤더의 `활동 로그`에서 로그인, 질문, 저장, 수정, 삭제 이력 조회

## 검색 설정

- `SIM_THRESHOLD`: 벡터 검색 최소 유사도. 높이면 누락이 늘고 오답이 줄어듭니다.
- `SIMILAR_MEMORY_THRESHOLD`: 저장 전 유사 기억 경고 기준입니다. 답변 검색 기준과 별도로 적용됩니다.
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
publishable key와 요청별 사용자 JWT로 실행해 RLS를 적용합니다. service key는 서버의
아이디-이메일/UUID 매핑, 감사 로그, 모두의 기억 공개·삭제 승인 대기 생성·조회와 명시적인
관리자 작업에만 사용합니다.

### Vercel 서버리스

루트 `main.py`의 FastAPI `app`을 Vercel이 하나의 Python Function으로 실행합니다.
Python 3.12, 서울 리전, Fluid Compute와 최대 실행 시간은 `.python-version`과
`vercel.json`에 정의되어 있습니다. `.vercelignore`는 `.env`, 가상환경, SQL과
테스트 파일이 CLI 업로드에 포함되지 않게 합니다.

```powershell
npx vercel@59.0.0 login
npx vercel@59.0.0 link
npx vercel@59.0.0 env add OPENAI_API_KEY production,preview --sensitive
npx vercel@59.0.0 env add SUPABASE_URL production,preview --sensitive
npx vercel@59.0.0 env add SUPABASE_PUBLISHABLE_KEY production,preview --sensitive
npx vercel@59.0.0 env add SUPABASE_SERVICE_KEY production,preview --sensitive
npx vercel@59.0.0 deploy
npx vercel@59.0.0 deploy --prod
```

사내 인증서 검사 환경에서 Node가 인증서를 거부하면 검증을 끄지 말고 현재
PowerShell 세션에 `$env:NODE_OPTIONS='--use-system-ca'`를 먼저 설정합니다.
운영에서는 회원가입이 기본 차단되고 기존 계정만 로그인할 수 있습니다. Vercel의
Production URL은 인터넷에서 접근 가능하므로 앱 인증을 유지하고, Preview는
Deployment Protection을 켠 뒤 공유합니다. 회사·팀의 지속 운영은 Vercel 요금제와
사내 클라우드 보안 정책을 확인해야 합니다.

### Docker 호스팅

Render, Railway 등 Docker를 지원하는 서비스에서는 저장소를 연결하고 다음 값을
환경변수로 등록합니다.

- `OPENAI_API_KEY`
- `SUPABASE_URL`
- `SUPABASE_PUBLISHABLE_KEY`
- `SUPABASE_SERVICE_KEY`
- `APP_ENV=production`
- `COOKIE_SECURE=true`
- `SIGNUP_ENABLED=false`

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
