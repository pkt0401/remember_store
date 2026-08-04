"""
Memory Agent — 복붙하면 저장, 물어보면 답변.

실행:  uvicorn main:app --reload --port 8000
접속:  http://localhost:8000
"""

import hashlib
import hmac
import json
import os
import uuid
from datetime import date, datetime, timezone
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from openai import OpenAI
from pydantic import BaseModel
from supabase import create_client

load_dotenv()

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]  # service_role key (로컬 전용)
APP_PASSWORD = os.getenv("APP_PASSWORD", "")  # 설정하면 로그인 필수 (배포 시 필수)
APP_SECRET = os.getenv("APP_SECRET", "")      # 세션 서명용 랜덤 문자열 (배포 시 필수)
CHAT_MODEL = os.getenv("CHAT_MODEL", "gpt-4o-mini")
EMBED_MODEL = os.getenv("EMBED_MODEL", "text-embedding-3-small")  # 1536 dims
SIM_THRESHOLD = float(os.getenv("SIM_THRESHOLD", "0.25"))  # 이보다 낮으면 "없음" 처리
DUP_THRESHOLD = float(os.getenv("DUP_THRESHOLD", "0.92"))  # 이 이상 유사하면 기존 기억을 교체
TOP_K = int(os.getenv("TOP_K", "8"))

sb = create_client(SUPABASE_URL, SUPABASE_KEY)
oai = OpenAI()  # OPENAI_API_KEY

app = FastAPI(title="Memory Agent")


# ---------- auth ----------
# APP_PASSWORD가 비어 있으면 인증 없이 동작 (로컬 모드).
# 설정돼 있으면 /api/* 접근에 세션 쿠키 필요 (배포 모드).

AUTH_ENABLED = bool(APP_PASSWORD)
if AUTH_ENABLED and len(APP_SECRET) < 16:
    raise RuntimeError("배포 모드에서는 APP_SECRET을 16자 이상 랜덤 문자열로 설정하세요.")

OPEN_PATHS = {"/", "/api/login", "/healthz"}


def make_session_token() -> str:
    return hmac.new(APP_SECRET.encode(), b"memory-agent-session-v1", hashlib.sha256).hexdigest()


def is_authed(request: Request) -> bool:
    cookie = request.cookies.get("ma_session", "")
    return bool(cookie) and hmac.compare_digest(cookie, make_session_token())


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if AUTH_ENABLED and path.startswith("/api/") and path not in OPEN_PATHS:
        if not is_authed(request):
            return JSONResponse({"detail": "로그인이 필요해요."}, status_code=401)
    return await call_next(request)


class LoginRequest(BaseModel):
    password: str


@app.post("/api/login")
def login(req: LoginRequest, response: Response):
    if not AUTH_ENABLED:
        return {"ok": True}
    if not hmac.compare_digest(req.password, APP_PASSWORD):
        raise HTTPException(401, "비밀번호가 맞지 않아요.")
    response.set_cookie(
        "ma_session",
        make_session_token(),
        max_age=60 * 60 * 24 * 30,  # 30일
        httponly=True,
        secure=True,
        samesite="lax",
    )
    return {"ok": True}


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


# ---------- models ----------

class IngestRequest(BaseModel):
    text: str


class AskRequest(BaseModel):
    question: str
    history: Optional[list[dict]] = None  # [{"role": "user"|"assistant", "content": "..."}]


# ---------- helpers ----------

def embed(texts: list[str]) -> list[list[float]]:
    resp = oai.embeddings.create(model=EMBED_MODEL, input=texts)
    return [d.embedding for d in resp.data]


PARSER_SYSTEM = """당신은 텍스트 파서입니다. 사용자가 붙여넣은 텍스트를 분석해
지식 저장소에 넣을 레코드로 변환합니다.

판별 규칙:
- Slack 복사본: "이름  오전/오후 H:MM" 또는 "Name  3:24 PM" 패턴이 반복되고,
  이모지 반응 수, 스레드 표시("N개의 댓글") 등이 섞여 있음 → source: "slack"
- 이메일: 제목/보낸사람/받는사람/날짜 헤더가 있거나, 인사말-본문-서명 구조 → source: "email"
- 둘 다 아니면 → source: "note"

변환 규칙:
1. Slack UI 잔여물(이모지 반응 카운트, "답장하기", 프로필 아이콘 텍스트 등)은 제거.
2. 화자와 발언은 유지: "홍길동: 내용" 형태로 정리.
3. 하나의 주제 단위(대략 300~800자)로 청크를 나눔. 짧으면 한 청크.
4. 청크가 3개 이상인 긴 스레드/메일이면, 전체 내용을 3~5문장으로 요약한
   summary 청크를 맨 앞에 추가 (metadata.is_summary = true).
5. metadata에 추출 가능한 것만 넣기: sender(주 화자/발신자), channel(추정 채널명),
   subject(메일 제목), msg_date(원문에 날짜가 있으면 "YYYY-MM-DD" 등 원문 표기 그대로),
   participants(대화 참여자 배열).
6. 시효 판단: 내용이 특정 날짜에 종속되면(회의 일정, 마감, 이벤트, 기간 한정 공지)
   해당 레코드의 expires_at을 "이벤트 종료일 + 7일"의 ISO 날짜("YYYY-MM-DDT23:59:59+09:00")로 설정.
   시효가 없는 정보(담당자, 정책, 프로세스, 일반 지식)는 expires_at을 null로.
7. 태그: 각 레코드에 tags 배열(1~4개)을 붙일 것. 프로젝트명, 조직, 주제 같은
   구체적 고유명사 위주 (예: "ATL", "일본법인", "배포일정", "AI PMO").
   "업무", "회의" 같은 너무 일반적인 단어는 금지.

반드시 아래 JSON만 출력. 마크다운 코드펜스 금지.
{
  "source": "slack" | "email" | "note",
  "records": [
    {"content": "...", "metadata": { ... }, "expires_at": "ISO날짜 또는 null", "tags": ["..."]}
  ]
}"""


def parse_pasted_text(text: str) -> dict:
    today = date.today().strftime("%Y-%m-%d (%A)")
    resp = oai.chat.completions.create(
        model=CHAT_MODEL,
        max_tokens=4000,
        response_format={"type": "json_object"},  # JSON 강제
        messages=[
            {"role": "system", "content": PARSER_SYSTEM + f"\n\n오늘 날짜: {today}. 본문에 '다음주 수요일' 같은 상대 날짜가 있으면 metadata.resolved_dates에 절대 날짜로 변환해 기록하세요 (본문은 수정 금지)."},
            {"role": "user", "content": text},
        ],
    )
    raw = resp.choices[0].message.content or ""
    raw = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        parsed = json.loads(raw)
        assert isinstance(parsed.get("records"), list) and parsed["records"]
        return parsed
    except (json.JSONDecodeError, AssertionError):
        # 파싱 실패 시 원문 그대로 note로 저장 (데이터 유실 방지)
        return {"source": "note", "records": [{"content": text.strip(), "metadata": {}}]}


ANSWER_SYSTEM = """당신은 사용자의 개인 메모리 저장소를 검색해 답하는 어시스턴트입니다.
아래 <검색결과>에 있는 내용만 근거로 답하세요.

규칙:
- 검색 결과에 근거가 없으면 "저장된 정보에서 찾지 못했다"고 솔직하게 답할 것. 추측 금지.
- 출처 표기, 대괄호, 메타데이터, 유사도 수치를 답변에 절대 포함하지 말 것. 자연스러운 문장으로만 답한다.
- "다음주", "내일" 같은 상대적 날짜는 오늘 날짜를 기준으로 계산해서 구체적 날짜로 답할 것.
- 검색 결과끼리 내용이 충돌하면 저장 날짜가 최신인 쪽을 우선하되, 충돌 사실을 한 문장으로 알릴 것.
- 한국어로 간결하게."""


# ---------- endpoints ----------

@app.post("/api/ingest")
def ingest(req: IngestRequest):
    text = req.text.strip()
    if not text:
        raise HTTPException(400, "빈 텍스트입니다.")

    parsed = parse_pasted_text(text)
    source = parsed.get("source", "note")
    records = parsed["records"]

    batch_id = str(uuid.uuid4())
    contents = [r["content"] for r in records]
    vectors = embed(contents)

    rows, replaced = [], 0
    for r, v in zip(records, vectors):
        # 중복 감지: 거의 같은 기억이 이미 있으면 옛것을 지우고 새것으로 교체
        dup = sb.rpc("match_memories", {
            "query_embedding": v, "match_count": 1,
        }).execute().data
        if dup and dup[0]["similarity"] >= DUP_THRESHOLD:
            sb.table("memories").delete().eq("id", dup[0]["id"]).execute()
            replaced += 1

        meta = r.get("metadata") or {}
        meta["batch_id"] = batch_id
        meta["tags"] = [t for t in (r.get("tags") or []) if isinstance(t, str)][:4]
        rows.append({
            "source": source,
            "content": r["content"],
            "metadata": meta,
            "embedding": v,
            "expires_at": r.get("expires_at") or None,
        })

    sb.table("memories").insert(rows).execute()

    return {
        "source": source,
        "saved": len(rows),
        "replaced": replaced,
        "batch_id": batch_id,
        "preview": [r["content"][:80] for r in records[:3]],
    }


def all_tags() -> dict[str, int]:
    res = sb.table("memories").select("metadata").execute()
    counts: dict[str, int] = {}
    for row in res.data or []:
        for t in (row.get("metadata") or {}).get("tags") or []:
            counts[t] = counts.get(t, 0) + 1
    return counts


@app.get("/api/tags")
def get_tags():
    counts = all_tags()
    return sorted(
        [{"tag": t, "count": c} for t, c in counts.items()],
        key=lambda x: -x["count"],
    )


@app.post("/api/ask")
def ask(req: AskRequest):
    question = req.question.strip()
    if not question:
        raise HTTPException(400, "질문이 비어 있습니다.")

    qvec = embed([question])[0]
    res = sb.rpc("match_memories", {
        "query_embedding": qvec,
        "match_count": TOP_K * 3,  # 넓게 가져와서 태그로 리랭킹
    }).execute()
    hits = res.data or []
    hits = [h for h in hits if h["similarity"] >= SIM_THRESHOLD]

    # 질문에 등장하는 알려진 태그 → 해당 태그 가진 기억에 가산점
    q_lower = question.lower()
    matched_tags = {t for t in all_tags() if t.lower() in q_lower}
    if matched_tags:
        for h in hits:
            tags = set((h.get("metadata") or {}).get("tags") or [])
            h["_score"] = h["similarity"] + 0.15 * len(tags & matched_tags)
        hits.sort(key=lambda h: -h["_score"])
    hits = hits[:TOP_K]

    if not hits:
        return {"answer": "저장된 정보에서 관련 내용을 찾지 못했어요. 먼저 관련 메시지를 붙여넣어 저장해 주세요.", "sources": []}

    def describe(h: dict) -> str:
        meta = h.get("metadata") or {}
        parts = [h["source"]]
        for key in ("sender", "channel", "subject", "msg_date"):
            if meta.get(key):
                parts.append(str(meta[key]))
        parts.append(f"저장일 {h['created_at'][:10]}")
        return " · ".join(parts)

    context = "\n\n".join(
        f"--- 출처: {describe(h)} ---\n{h['content']}" for h in hits
    )
    today = date.today().strftime("%Y-%m-%d (%A)")

    messages = [{"role": "system", "content": ANSWER_SYSTEM + f"\n\n오늘 날짜: {today}"}]
    for turn in (req.history or [])[-6:]:
        if turn.get("role") in ("user", "assistant") and turn.get("content"):
            messages.append({"role": turn["role"], "content": turn["content"]})
    messages.append({
        "role": "user",
        "content": f"<검색결과>\n{context}\n</검색결과>\n\n질문: {question}",
    })

    resp = oai.chat.completions.create(
        model=CHAT_MODEL,
        max_tokens=1500,
        messages=messages,
    )
    answer = resp.choices[0].message.content or ""

    return {
        "answer": answer,
        "sources": [
            {
                "id": h["id"],
                "source": h["source"],
                "metadata": h["metadata"],
                "similarity": round(h["similarity"], 3),
                "snippet": h["content"][:120],
                "content": h["content"],
            }
            for h in hits[:5]
        ],
    }


@app.get("/api/memories")
def list_memories(limit: int = 30):
    res = (
        sb.table("memories")
        .select("id, source, content, metadata, created_at, expires_at")
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    return res.data


@app.delete("/api/memories/expired")
def delete_expired():
    now = datetime.now(timezone.utc).isoformat()
    res = sb.table("memories").delete().lt("expires_at", now).execute()
    return {"deleted": len(res.data or [])}


@app.delete("/api/memories/{memory_id}")
def delete_memory(memory_id: str):
    sb.table("memories").delete().eq("id", memory_id).execute()
    return {"deleted": memory_id}


app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
def index():
    return FileResponse("static/index.html")
