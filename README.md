# 기억 서랍 (Memory Agent)

Slack 메시지·메일·메모를 붙여넣으면 자동으로 분류/청킹/임베딩해서 Supabase에 저장하고,
나중에 질문하면 저장된 내용만 근거로 답하는 로컬 웹앱.

## 구조

```
붙여넣기 → Claude 파서 (slack/email/note 판별 + 청킹 + 메타데이터 추출)
        → OpenAI 임베딩 → Supabase (pgvector)

질문     → 임베딩 → match_memories (코사인 검색, top-k)
        → 유사도 임계값 미달이면 "없음" 응답 → Claude 답변 + 출처 표시
```

## 세팅 (10분)

1. **Supabase**: 새 프로젝트 생성 → SQL Editor에서 `schema.sql` 전체 실행
2. **키 준비**: Supabase URL + service_role key (Settings → API),
   Anthropic API 키, OpenAI API 키
3. **환경변수**: `cp .env.example .env` 후 값 채우기
4. **실행**:

```bash
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

→ http://localhost:8000

## 사용법

- **넣기**: Slack에서 스레드 드래그 복사 → 왼쪽 박스에 붙여넣기 → 저장 (Ctrl/⌘+Enter)
  - 사내 메일도 본문 복사해서 동일하게
  - 그냥 메모("A프로젝트 담당자는 김OO") 도 됨
- **묻기**: 오른쪽 채팅에서 질문. 답변 아래 칩에 마우스를 올리면 근거 스니펫 표시
- **관리**: 최근 저장 목록에서 × 로 개별 삭제

## 조정 포인트

- `SIM_THRESHOLD` (기본 0.25): 높이면 "모름" 응답이 늘고 오답이 줄어듦
- `PARSER_SYSTEM` / `ANSWER_SYSTEM` 프롬프트: main.py 상단에서 수정
- 긴 스레드는 요약 청크가 자동으로 함께 저장됨 (`metadata.is_summary`)

## 주의

- service_role 키는 RLS를 우회하므로 **로컬 실행 전용**. 배포하려면 anon key + RLS 정책으로 전환할 것.
- 사내 메일/메시지를 외부 API(Anthropic/OpenAI/Supabase)로 보내는 구조이므로, 회사 보안 정책 확인 권장.

## 배포 (항상 켜두기)

로컬에서는 `APP_PASSWORD`를 비워두면 로그인 없이 동작하고,
배포할 땐 반드시 설정해야 합니다. 미설정 상태로 배포하면 URL을 아는 누구나 데이터를 읽고 쓸 수 있어요.

### 공통 준비

환경변수 2개 추가:
- `APP_PASSWORD`: 접속 비밀번호 (본인만 아는 값)
- `APP_SECRET`: 세션 서명용 랜덤 문자열 — `python -c "import secrets; print(secrets.token_hex(24))"` 로 생성

### Render (무료 티어)

1. 이 폴더를 GitHub 저장소로 push (`.env`는 .gitignore — 절대 커밋 금지)
2. render.com → New → Web Service → 저장소 연결
3. Runtime: Docker 자동 감지됨
4. Environment 탭에 `.env`의 모든 값 입력 (SUPABASE_URL, SUPABASE_SERVICE_KEY, OPENAI_API_KEY, APP_PASSWORD, APP_SECRET)
5. 배포 완료 → `https://xxx.onrender.com` 접속 → 비밀번호 입력

무료 티어는 15분 미접속 시 잠들었다가 첫 요청에 ~30초 걸려 깨어남. 개인용으론 충분.

### Railway (월 $5, 안 잠듦)

railway.app → New Project → Deploy from GitHub → 저장소 선택 → Variables에 환경변수 입력. 나머지 동일.

### 배포 후 확인

- `https://.../healthz` 가 `{"status":"ok"}` 반환하면 서버 정상
- 첫 접속 시 비밀번호 화면이 뜨는지 확인 (안 뜨면 APP_PASSWORD 미설정 상태!)
- 로그인 세션은 30일 유지, 폰 브라우저에서도 동일하게 사용 가능
