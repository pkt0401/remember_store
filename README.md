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
- `오늘`, `어제`, `이번 주`, `지난주`, `이번 달` 기반 날짜 검색
- 중복 기억 교체, 만료된 기억 정리, 개별 삭제
- 저장된 본문과 담당자·프로젝트·상태·날짜·유형·태그 수정 및 재임베딩
- 답변 근거 원문 조회
- 브라우저 `localStorage` 기반 대화 이력 복원 및 전체 삭제
- 선택적 비밀번호 로그인

## 처리 구조

```text
붙여넣기
  -> OpenAI 텍스트 분석 및 구조화
  -> OpenAI 임베딩 생성
  -> Supabase PostgreSQL + pgvector 저장

질문
  -> 이름·날짜 정확 검색 + 벡터 유사도 검색
  -> 태그 리랭킹
  -> OpenAI 답변 생성
  -> 답변과 근거 원문 표시
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

Supabase SQL Editor에서 다음 파일을 순서대로 실행합니다.

1. `schema.sql`
2. `migration_expiry.sql`로 만료 필드와 검색 함수를 갱신

`SUPABASE_SERVICE_KEY`는 RLS를 우회하므로 개인 로컬 실행 용도로만 사용해야 합니다.

## 환경변수

프로젝트 루트에 `.env`를 만듭니다.

```dotenv
OPENAI_API_KEY=your-openai-api-key
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_SERVICE_KEY=your-supabase-service-role-key

# 선택 사항
CHAT_MODEL=gpt-4o-mini
EMBED_MODEL=text-embedding-3-small
SIM_THRESHOLD=0.25
DUP_THRESHOLD=0.92
TOP_K=8
APP_PASSWORD=
APP_SECRET=
```

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

## 검색 설정

- `SIM_THRESHOLD`: 벡터 검색 최소 유사도. 높이면 누락이 늘고 오답이 줄어듭니다.
- `DUP_THRESHOLD`: 기존 기억을 중복으로 판단해 교체하는 유사도입니다.
- `TOP_K`: 답변 생성에 사용할 최대 검색 결과 수입니다.
- `PARSER_SYSTEM`: 저장 시 구조화 규칙입니다.
- `ANSWER_SYSTEM`: 저장된 정보로 답변하는 규칙입니다.

## 포트 확인과 종료

```bash
ss -ltnp | grep ':8000'
fuser -v 8000/tcp
fuser -k 8000/tcp
```

현재 터미널에서 실행한 Uvicorn은 `Ctrl+C`로 종료합니다.

## 배포

로컬에서는 `APP_PASSWORD`가 비어 있으면 로그인 없이 동작합니다. 외부에 배포할
때는 반드시 `APP_PASSWORD`와 16자 이상의 `APP_SECRET`을 설정합니다.

```bash
python -c "import secrets; print(secrets.token_hex(24))"
```

Render, Railway 등 Docker를 지원하는 서비스에서는 저장소를 연결하고 다음 값을
환경변수로 등록합니다.

- `OPENAI_API_KEY`
- `SUPABASE_URL`
- `SUPABASE_SERVICE_KEY`
- `APP_PASSWORD`
- `APP_SECRET`

배포 후 `/healthz`가 `{"status":"ok"}`를 반환하는지 확인합니다.

## 보안 주의사항

- `.env`는 Git에 커밋하지 않습니다.
- API 키가 로그나 대화에 노출되면 즉시 폐기하고 새로 발급합니다.
- 저장 내용과 질문은 처리 과정에서 OpenAI와 Supabase로 전송됩니다.
- API 키나 비밀번호를 기억으로 저장할 수 있지만 외부 API로 전송된다는 점을
  이해한 경우에만 사용해야 합니다.
- 외부 배포에서는 service role 키 대신 사용자 인증과 RLS 정책 적용을 권장합니다.
