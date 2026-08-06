"""
Memory Agent — 복붙하면 저장, 물어보면 답변.

실행:  uvicorn main:app --reload --port 8000
접속:  http://localhost:8000
"""

import hashlib
import hmac
import json
import logging
import os
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from openai import OpenAI
from pydantic import BaseModel
from supabase import create_client

load_dotenv()

logger = logging.getLogger(__name__)

SUPABASE_URL = os.environ["SUPABASE_URL"].strip()
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"].strip()  # service_role key (로컬 전용)
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"].strip()
APP_PASSWORD = os.getenv("APP_PASSWORD", "")  # 설정하면 로그인 필수 (배포 시 필수)
APP_SECRET = os.getenv("APP_SECRET", "")      # 세션 서명용 랜덤 문자열 (배포 시 필수)
CHAT_MODEL = os.getenv("CHAT_MODEL", "gpt-4o-mini")
EMBED_MODEL = os.getenv("EMBED_MODEL", "text-embedding-3-small")  # 1536 dims
SIM_THRESHOLD = float(os.getenv("SIM_THRESHOLD", "0.25"))  # 이보다 낮으면 "없음" 처리
DUP_THRESHOLD = float(os.getenv("DUP_THRESHOLD", "0.92"))  # 이 이상 유사하면 기존 기억을 교체
TOP_K = int(os.getenv("TOP_K", "8"))

sb = create_client(SUPABASE_URL, SUPABASE_KEY)
oai = OpenAI(api_key=OPENAI_API_KEY)

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


class UpdateMemoryRequest(BaseModel):
    content: str
    metadata: Optional[dict] = None


class AskRequest(BaseModel):
    question: str
    history: Optional[list[dict]] = None  # [{"role": "user"|"assistant", "content": "..."}]


# ---------- helpers ----------

def embed(texts: list[str]) -> list[list[float]]:
    resp = oai.embeddings.create(model=EMBED_MODEL, input=texts)
    return [d.embedding for d in resp.data]


VALID_STATUSES = {"할 일", "진행 중", "완료", "보류", "참고"}
VALID_RECORD_TYPES = {"work", "credential", "system", "link", "course", "note"}
SECTION_TYPES = {
    "work": {"work"},
    "access": {"credential", "system"},
    "resources": {"link", "course"},
    "notes": {"note"},
}


def iso_date(value: object) -> Optional[str]:
    if not isinstance(value, str):
        return None
    match = re.search(r"\d{4}-\d{2}-\d{2}", value)
    return match.group(0) if match else None


def normalize_metadata(record: dict) -> dict:
    meta = record.get("metadata") or {}
    sender = str(meta.get("sender") or "").strip()
    person = str(meta.get("person") or sender).strip()
    status = str(meta.get("status") or "참고").strip()
    meta["person"] = person
    meta["project"] = str(meta.get("project") or "").strip()
    meta["status"] = status if status in VALID_STATUSES else "참고"
    meta["work_date"] = (
        iso_date(meta.get("work_date"))
        or iso_date(meta.get("msg_date"))
        or date.today().isoformat()
    )
    meta["due_date"] = iso_date(meta.get("due_date"))
    meta["category"] = str(meta.get("category") or "정보").strip()
    record_type = str(meta.get("record_type") or "").strip()
    if record_type not in VALID_RECORD_TYPES:
        record_type = infer_record_type(record.get("content") or "", meta)
    meta["record_type"] = record_type
    return meta


def infer_record_type(content: str, meta: Optional[dict] = None) -> str:
    meta = meta or {}
    lowered = content.lower()
    if "[서비스 계정]" in content or "비밀번호:" in content or "임시pw" in lowered:
        return "credential"
    if "[서버 접속]" in content or "[개발 환경]" in content or "호스트:" in content:
        return "system"
    if "[강의 과정]" in content or ("과정명:" in content and "강의 목록:" in content):
        return "course"
    if "[참고 링크]" in content or "시연 영상" in content:
        return "link"
    category = str(meta.get("category") or "")
    if category in {"업무", "일정", "의사결정"} or meta.get("sender"):
        return "work"
    return "note"


def effective_metadata(item: dict) -> dict:
    meta = dict(item.get("metadata") or {})
    tags = [tag for tag in meta.get("tags") or [] if isinstance(tag, str)]
    content = item.get("content") or ""
    if not meta.get("person"):
        meta["person"] = str(meta.get("sender") or "").strip()
    if not meta.get("project"):
        project_tags = [tag for tag in tags if "Innovation" not in tag and "팀" not in tag]
        meta["project"] = project_tags[0] if project_tags else (tags[0] if tags else "")
    if not meta.get("status"):
        if "진행 중" in content:
            meta["status"] = "진행 중"
        elif "할 일" in content:
            meta["status"] = "할 일"
        elif "완료" in content:
            meta["status"] = "완료"
        else:
            meta["status"] = "참고"
    if not iso_date(meta.get("work_date")):
        meta["work_date"] = iso_date(meta.get("msg_date")) or item["created_at"][:10]
    meta.setdefault("due_date", None)
    meta.setdefault("category", "정보")
    record_type = str(meta.get("record_type") or "")
    meta["record_type"] = (
        record_type if record_type in VALID_RECORD_TYPES
        else infer_record_type(content, meta)
    )
    return meta


def question_date_range(question: str) -> Optional[tuple[date, date]]:
    today = date.today()
    if "오늘" in question:
        return today, today
    if "어제" in question:
        yesterday = today - timedelta(days=1)
        return yesterday, yesterday
    if "지난주" in question or "저번주" in question:
        this_monday = today - timedelta(days=today.weekday())
        return this_monday - timedelta(days=7), this_monday - timedelta(days=1)
    if "이번 주" in question or "이번주" in question:
        monday = today - timedelta(days=today.weekday())
        return monday, monday + timedelta(days=6)
    if "이번 달" in question or "이번달" in question:
        month_start = today.replace(day=1)
        next_month = (month_start.replace(day=28) + timedelta(days=4)).replace(day=1)
        return month_start, next_month - timedelta(days=1)
    return None


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
5. metadata에 다음 구조화 필드를 가능한 한 추출:
   - sender: 주 화자/발신자, person: 업무 담당자
   - project: 프로젝트명, category: 업무/일정/의사결정/참고자료 중 하나
   - record_type: work/credential/system/link/course/note 중 하나
     [서비스 계정]은 credential, [서버 접속]과 [개발 환경]은 system,
     [참고 링크]는 link, [강의 과정]은 course, 일반 업무 보고는 work로 분류
   - status: "할 일"/"진행 중"/"완료"/"보류"/"참고" 중 하나
   - work_date: 실제 업무 기준일 YYYY-MM-DD, due_date: 마감일 YYYY-MM-DD 또는 null
   - channel, subject, msg_date, participants
   날짜가 없으면 work_date는 오늘 날짜를 사용. 한 청크에 상태가 섞이면 상태별로 청크를 분리.
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

    try:
        parsed = parse_pasted_text(text)
    except Exception:
        logger.exception("Failed to parse pasted text")
        raise HTTPException(502, "AI 텍스트 분석에 실패했습니다. 서버 로그를 확인하세요.")
    source = parsed.get("source", "note")
    records = parsed["records"]

    batch_id = str(uuid.uuid4())
    contents = [r["content"] for r in records]
    try:
        vectors = embed(contents)
    except Exception:
        logger.exception("Failed to create embeddings")
        raise HTTPException(502, "AI 임베딩 생성에 실패했습니다. 서버 로그를 확인하세요.")

    rows, replaced = [], 0
    try:
        for r, v in zip(records, vectors):
            # 중복 감지: 거의 같은 기억이 이미 있으면 옛것을 지우고 새것으로 교체
            dup = sb.rpc("match_memories", {
                "query_embedding": v, "match_count": 1,
            }).execute().data
            if dup and dup[0]["similarity"] >= DUP_THRESHOLD:
                sb.table("memories").delete().eq("id", dup[0]["id"]).execute()
                replaced += 1

            meta = normalize_metadata(r)
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
    except Exception:
        logger.exception("Failed to write memories to Supabase")
        raise HTTPException(502, "기억 저장소 연결 또는 저장에 실패했습니다. 서버 로그를 확인하세요.")

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

    # 사람 이름처럼 짧고 고유한 검색어는 임베딩 유사도가 낮을 수 있으므로
    # 본문 정확 일치 결과를 벡터 검색보다 우선해서 합친다.
    names = set(re.findall(r"([가-힣]{2,4})\s*(?:매니저|님)", question))
    exact_hits = []
    for name in names:
        result = (
            sb.table("memories")
            .select("id,source,content,metadata,created_at")
            .ilike("content", f"%{name}%")
            .limit(TOP_K)
            .execute()
        )
        for row in result.data or []:
            row["similarity"] = 1.0
            exact_hits.append(row)

    seen_ids = {h["id"] for h in exact_hits}
    hits = exact_hits + [h for h in hits if h["id"] not in seen_ids]

    date_range = question_date_range(question)
    if date_range:
        start, end = (value.isoformat() for value in date_range)
        dated = sb.table("memories").select(
            "id,source,content,metadata,created_at"
        ).limit(200).execute().data or []
        for row in dated:
            meta = row.get("metadata") or {}
            work_date = iso_date(meta.get("work_date")) or row["created_at"][:10]
            if start <= work_date <= end and row["id"] not in seen_ids:
                row["similarity"] = 0.9
                hits.insert(0, row)
                seen_ids.add(row["id"])

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
def list_memories(
    limit: int = 100,
    person: Optional[str] = None,
    project: Optional[str] = None,
    status: Optional[str] = None,
    section: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
):
    res = (
        sb.table("memories")
        .select("id, source, content, metadata, created_at, expires_at")
        .order("created_at", desc=True)
        .limit(500)
        .execute()
    )
    items = res.data or []

    def matches(item: dict) -> bool:
        meta = effective_metadata(item)
        item_person = str(meta.get("person") or "")
        item_project = str(meta.get("project") or "")
        item_status = str(meta.get("status") or "참고")
        work_date = iso_date(meta.get("work_date")) or item["created_at"][:10]
        return (
            (not person or item_person == person)
            and (not project or item_project == project)
            and (not status or item_status == status)
            and (not section or meta.get("record_type") in SECTION_TYPES.get(section, set()))
            and (not date_from or work_date >= date_from)
            and (not date_to or work_date <= date_to)
        )

    filtered = [item for item in items if matches(item)]
    for item in filtered:
        item["metadata"] = effective_metadata(item)
    filtered.sort(
        key=lambda item: (
            iso_date((item.get("metadata") or {}).get("work_date"))
            or item["created_at"][:10],
            item["created_at"],
        ),
        reverse=True,
    )
    return filtered[:min(max(limit, 1), 500)]


@app.get("/api/memory-filters")
def memory_filters():
    rows = sb.table("memories").select(
        "content,metadata,created_at"
    ).limit(1000).execute().data or []

    def values(key: str, fallback: Optional[str] = None) -> list[str]:
        result = set()
        for row in rows:
            meta = effective_metadata(row)
            value = meta.get(key) or (meta.get(fallback) if fallback else None)
            if isinstance(value, str) and value.strip():
                result.add(value.strip())
        return sorted(result)

    return {
        "people": values("person", "sender"),
        "projects": values("project"),
        "statuses": sorted(VALID_STATUSES),
    }


@app.delete("/api/memories/expired")
def delete_expired():
    now = datetime.now(timezone.utc).isoformat()
    res = sb.table("memories").delete().lt("expires_at", now).execute()
    return {"deleted": len(res.data or [])}


@app.patch("/api/memories/{memory_id}")
def update_memory(memory_id: str, req: UpdateMemoryRequest):
    content = req.content.strip()
    if not content:
        raise HTTPException(400, "본문은 비워둘 수 없습니다.")

    existing = (
        sb.table("memories")
        .select("id,source,content,metadata,created_at,expires_at")
        .eq("id", memory_id)
        .limit(1)
        .execute()
        .data
    )
    if not existing:
        raise HTTPException(404, "수정할 기억을 찾지 못했습니다.")

    editable_keys = {
        "person", "project", "status", "work_date", "due_date",
        "category", "record_type", "tags", "sender", "subject", "channel",
    }
    meta = dict(existing[0].get("metadata") or {})
    for key, value in (req.metadata or {}).items():
        if key in editable_keys:
            meta[key] = value

    record = {"content": content, "metadata": meta}
    meta = normalize_metadata(record)
    meta["tags"] = [
        tag.strip() for tag in meta.get("tags") or []
        if isinstance(tag, str) and tag.strip()
    ][:8]

    try:
        vector = embed([content])[0]
        result = (
            sb.table("memories")
            .update({"content": content, "metadata": meta, "embedding": vector})
            .eq("id", memory_id)
            .execute()
        )
    except Exception:
        logger.exception("Failed to update memory")
        raise HTTPException(502, "기억 수정에 실패했습니다. 서버 로그를 확인하세요.")

    return (result.data or [{"id": memory_id, "content": content, "metadata": meta}])[0]


@app.delete("/api/memories/{memory_id}")
def delete_memory(memory_id: str):
    sb.table("memories").delete().eq("id", memory_id).execute()
    return {"deleted": memory_id}


app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
def index():
    return FileResponse("static/index.html")
