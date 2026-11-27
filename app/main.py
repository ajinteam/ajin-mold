import base64
import hashlib
import hmac
import io
import json
import re
import os
import secrets
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from app.excel_parser import parse_mold_workbook
from PIL import Image, ImageOps
from pydantic import BaseModel
from supabase import Client, create_client

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY", os.getenv("SUPABASE_KEY", "")).strip()
STORAGE_BUCKET = os.getenv("SUPABASE_STORAGE_BUCKET", "mold-photos").strip()
APP_SECRET = os.getenv("APP_SECRET", "change-this-secret-key")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("환경변수 SUPABASE_URL 및 SUPABASE_SERVICE_KEY(또는 SUPABASE_KEY)를 설정해 주세요.")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

app = FastAPI(title="금형 QR · 위치 · 사진 관리 시스템")
BASE_DIR = Path(__file__).resolve().parent.parent
INDEX_CANDIDATES = [
    BASE_DIR / "templates" / "index.html",
    BASE_DIR / "index.html",
    Path(__file__).resolve().parent / "index.html",
]


class RestorePathMiddleware:
    """Vercel이 요청 경로를 /api/index(.py) 로 바꿔 전달해도, ?__path=원래경로 값으로 복원한다.
    (프론트의 모든 API 호출이 /api/index?__path=/api/... 형태로 오므로 Vercel 설정과 무관하게 동작)"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        qs_raw = scope.get("query_string", b"") or b""
        if scope.get("type") in ("http", "websocket") and (
            b"__path=" in qs_raw or scope.get("path") in ("/api/index", "/api/index.py")
        ):
            from urllib.parse import parse_qsl, urlencode

            pairs = parse_qsl(scope.get("query_string", b"").decode("utf-8"), keep_blank_values=True)
            orig = None
            rest = []
            for k, v in pairs:
                if k == "__path" and orig is None:
                    orig = v
                else:
                    rest.append((k, v))
            new_path = "/" + (orig or "").lstrip("/")
            scope = dict(scope)
            scope["path"] = new_path
            scope["raw_path"] = new_path.encode("utf-8")
            scope["query_string"] = urlencode(rest).encode("utf-8")
        await self.app(scope, receive, send)


app.add_middleware(RestorePathMiddleware)


# 메모리 기반 간단 세션 토큰 저장소 (단일 인스턴스 기준, 필요시 DB/Redis로 확장 가능)
SESSIONS: Dict[str, Dict[str, Any]] = {}


def hash_password(raw_pw: str) -> str:
    return hashlib.sha256(raw_pw.encode("utf-8")).hexdigest()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# 서버리스(Vercel)는 요청마다 다른 인스턴스에서 실행될 수 있으므로,
# 메모리 세션 대신 서명된 토큰(HMAC)을 사용한다.
TOKEN_TTL_SEC = 12 * 3600
_SIGN_KEY = hashlib.sha256((APP_SECRET + "|" + SUPABASE_KEY).encode("utf-8")).digest()


def make_token(user_info: Dict[str, Any]) -> str:
    payload = dict(user_info)
    payload["exp"] = int(time.time()) + TOKEN_TTL_SEC
    body = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).decode("ascii").rstrip("=")
    sig = hmac.new(_SIGN_KEY, body.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def read_token(token: str) -> Optional[Dict[str, Any]]:
    try:
        body, sig = token.rsplit(".", 1)
        expect = hmac.new(_SIGN_KEY, body.encode("ascii"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expect):
            return None
        payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)).decode("utf-8"))
        if int(payload.get("exp", 0)) < time.time():
            return None
        payload.pop("exp", None)
        return payload
    except Exception:
        return None


def get_current_user(
    authorization: Optional[str] = Header(default=None),
    x_auth_token: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    token = (x_auth_token or "").strip()
    if not token and authorization and authorization.startswith("Bearer "):
        token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(status_code=401, detail="로그인이 필요합니다.")
    session = read_token(token)
    if not session:
        raise HTTPException(status_code=401, detail="세션이 만료되었습니다. 다시 로그인해 주세요.")
    rows = (
        supabase.table("users")
        .select("id, username, full_name, initial, role, assigned_location, is_active")
        .eq("id", session.get("id"))
        .execute()
        .data
        or []
    )
    if not rows or not rows[0].get("is_active"):
        raise HTTPException(status_code=401, detail="비활성화되었거나 삭제된 계정입니다.")
    u = rows[0]
    return {k: u.get(k) for k in ("id", "username", "full_name", "initial", "role", "assigned_location")}


def require_master(user: Dict[str, Any] = Depends(get_current_user)) -> Dict[str, Any]:
    if user.get("role") != "master":
        raise HTTPException(status_code=403, detail="마스터 권한만 실행할 수 있습니다.")
    return user


# ------------------ Pydantic 스키마 ------------------

class LoginRequest(BaseModel):
    username: str
    password: str


class LocationCreate(BaseModel):
    name: str
    loc_type: str = "vendor"  # 'vendor' | 'internal'


class UserCreate(BaseModel):
    username: str
    password: str
    full_name: str
    initial: str
    role: str = "staff"  # 'master' | 'staff' | 'vendor'
    assigned_location: Optional[str] = None


class UserUpdate(BaseModel):
    full_name: Optional[str] = None
    initial: Optional[str] = None
    role: Optional[str] = None
    assigned_location: Optional[str] = None
    is_active: Optional[bool] = None
    new_password: Optional[str] = None


class LocationUpdate(BaseModel):
    name: Optional[str] = None
    loc_type: Optional[str] = None
    is_active: Optional[bool] = None


class BulkMoveRequest(BaseModel):
    mold_ids: List[int]
    to_warehouse: str
    core_status: Optional[str] = None
    memo: str = ""


class MemoUpdateRequest(BaseModel):
    memo: str
    core_status: Optional[str] = None


# ------------------ 페이지 & 인증 ------------------

PATH_WRAPPER_SCRIPT = r"""<script>
(function () {
  if (window.__pathWrapped) return;
  window.__pathWrapped = true;
  var _f = window.fetch.bind(window);
  window.fetch = function (url, opts) {
    try {
      if (typeof url === "string" && url.indexOf("/api/") === 0 && url.indexOf("/api/index") !== 0) {
        var hh = Object.assign({}, (opts && opts.headers) || {});
        var au = hh["Authorization"] || hh["authorization"];
        if (au) hh["X-Auth-Token"] = String(au).replace(/^Bearer\s+/i, "");
        opts = Object.assign({}, opts || {}, { headers: hh });
        var u = new URL(url, location.origin);
        var p = new URLSearchParams(u.search);
        p.set("__path", u.pathname);
        url = "/api/index?" + p.toString();
      }
    } catch (e) {}
    return _f(url, opts);
  };
})();
</script>"""


@app.get("/", response_class=HTMLResponse)
def index():
    for p in INDEX_CANDIDATES:
        if p.exists():
            html = p.read_text(encoding="utf-8")
            # 화면 파일이 예전 버전이어도 항상 경로 보정 스크립트를 먼저 실행시킨다
            if "<head>" in html:
                html = html.replace("<head>", "<head>\n" + PATH_WRAPPER_SCRIPT, 1)
            else:
                html = PATH_WRAPPER_SCRIPT + html
            return HTMLResponse(html, headers={"Cache-Control": "no-store, max-age=0", "Permissions-Policy": "camera=(self)"})
    raise HTTPException(status_code=500, detail="index.html 파일을 찾을 수 없습니다. templates/index.html 위치를 확인하세요.")


@app.post("/api/auth/login")
def login(payload: LoginRequest):
    pw_hash = hash_password(payload.password)
    res = (
        supabase.table("users")
        .select("id, username, full_name, initial, role, assigned_location, is_active")
        .eq("username", payload.username.strip())
        .eq("password_hash", pw_hash)
        .execute()
    )
    rows = res.data or []
    if not rows:
        raise HTTPException(status_code=401, detail="아이디 또는 비밀번호가 올바르지 않습니다.")
    u = rows[0]
    if not u.get("is_active"):
        raise HTTPException(status_code=403, detail="비활성화된 계정입니다. 마스터에게 문의하세요.")

    user_info = {
        "id": u["id"],
        "username": u["username"],
        "full_name": u["full_name"],
        "initial": u["initial"],
        "role": u["role"],
        "assigned_location": u.get("assigned_location"),
    }
    token = make_token(user_info)
    return {"token": token, "user": user_info}


MIN_PW_LEN = 4


@app.get("/api/auth/me")
def me(user: Dict[str, Any] = Depends(get_current_user)):
    return {"user": user}


# ------------------ 마스터: 사용자 및 외부업체 설정 ------------------

@app.get("/api/locations")
def list_locations(user: Dict[str, Any] = Depends(get_current_user)):
    q = supabase.table("locations").select("id, name, loc_type, is_active").eq("is_active", True).order("name")
    rows = q.execute().data or []
    if user.get("role") == "vendor" and user.get("assigned_location"):
        # 외부 업체는 자사 보관위치 또는 이동 가능 위치 조회
        return {"locations": rows}
    return {"locations": rows}


@app.get("/api/admin/locations")
def admin_list_locations(_: Dict[str, Any] = Depends(require_master)):
    rows = supabase.table("locations").select("id, name, loc_type, is_active, created_at").order("id").execute().data or []
    return {"locations": rows}


@app.post("/api/admin/locations")
def admin_create_location(payload: LocationCreate, _: Dict[str, Any] = Depends(require_master)):
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="업체/보관위치 이름을 입력해 주세요.")
    loc_type = payload.loc_type if payload.loc_type in ("vendor", "internal") else "vendor"
    existing = supabase.table("locations").select("id, is_active").eq("name", name).execute().data or []
    if existing:
        loc_id = existing[0]["id"]
        updated = supabase.table("locations").update({"loc_type": loc_type, "is_active": True}).eq("id", loc_id).execute().data
        return {"location": updated[0]}
    created = supabase.table("locations").insert({"name": name, "loc_type": loc_type, "is_active": True}).execute().data
    return {"location": created[0]}


@app.get("/api/admin/users")
def admin_list_users(_: Dict[str, Any] = Depends(require_master)):
    rows = (
        supabase.table("users")
        .select("id, username, full_name, initial, role, assigned_location, is_active, created_at")
        .order("id")
        .execute()
        .data
        or []
    )
    return {"users": rows}


@app.post("/api/admin/users")
def admin_create_user(payload: UserCreate, _: Dict[str, Any] = Depends(require_master)):
    username = payload.username.strip()
    initial = payload.initial.strip().upper()
    if not username or not payload.password or not initial:
        raise HTTPException(status_code=400, detail="아이디, 비밀번호, 이니셜은 필수입니다.")
    role = payload.role if payload.role in ("master", "staff", "vendor") else "staff"
    assigned_loc = payload.assigned_location.strip() if payload.assigned_location else None
    if role == "vendor" and not assigned_loc:
        raise HTTPException(status_code=400, detail="외부업체 계정은 담당 보관위치(업체명)를 선택해야 합니다.")

    # 외부업체명이 locations에 없으면 자동 등록
    if assigned_loc:
        loc_exists = supabase.table("locations").select("id").eq("name", assigned_loc).execute().data or []
        if not loc_exists:
            supabase.table("locations").insert({"name": assigned_loc, "loc_type": "vendor", "is_active": True}).execute()

    dup = supabase.table("users").select("id").eq("username", username).execute().data or []
    if dup:
        raise HTTPException(status_code=409, detail="이미 존재하는 아이디입니다.")

    inserted = (
        supabase.table("users")
        .insert(
            {
                "username": username,
                "password_hash": hash_password(payload.password),
                "full_name": payload.full_name.strip() or username,
                "initial": initial,
                "role": role,
                "assigned_location": assigned_loc if role == "vendor" else assigned_loc,
                "is_active": True,
            }
        )
        .execute()
        .data
    )
    u = inserted[0]
    u.pop("password_hash", None)
    return {"user": u}


def _active_master_count(exclude_id: Optional[int] = None) -> int:
    rows = supabase.table("users").select("id").eq("role", "master").eq("is_active", True).execute().data or []
    return len([r for r in rows if r["id"] != exclude_id])


def _count_rows(table: str, column: str, value: str) -> int:
    res = supabase.table(table).select("id", count="exact").eq(column, value).limit(1).execute()
    if getattr(res, "count", None) is not None:
        return int(res.count)
    return len(res.data or [])


@app.patch("/api/admin/users/{user_id}")
def admin_update_user(user_id: int, payload: UserUpdate, me: Dict[str, Any] = Depends(require_master)):
    cur_rows = (
        supabase.table("users").select("id, role, assigned_location, is_active").eq("id", user_id).execute().data or []
    )
    if not cur_rows:
        raise HTTPException(status_code=404, detail="사용자를 찾을 수 없습니다.")
    cur = cur_rows[0]

    patch: Dict[str, Any] = {}
    if payload.full_name is not None:
        if not payload.full_name.strip():
            raise HTTPException(status_code=400, detail="이름은 비워둘 수 없습니다.")
        patch["full_name"] = payload.full_name.strip()
    if payload.initial is not None and payload.initial.strip():
        patch["initial"] = payload.initial.strip().upper()

    new_role = cur["role"]
    if payload.role in ("master", "staff", "vendor"):
        patch["role"] = payload.role
        new_role = payload.role

    new_loc = cur.get("assigned_location")
    if payload.assigned_location is not None:
        new_loc = payload.assigned_location.strip() or None
        patch["assigned_location"] = new_loc
    if new_role == "vendor" and not new_loc:
        raise HTTPException(status_code=400, detail="외부업체 계정은 담당 보관위치(업체명)를 선택해야 합니다.")

    new_active = bool(cur["is_active"])
    if payload.is_active is not None:
        patch["is_active"] = payload.is_active
        new_active = payload.is_active

    if payload.new_password:
        if len(payload.new_password) < MIN_PW_LEN:
            raise HTTPException(status_code=400, detail=f"비밀번호는 {MIN_PW_LEN}자 이상이어야 합니다.")
        patch["password_hash"] = hash_password(payload.new_password)

    if not patch:
        raise HTTPException(status_code=400, detail="변경할 항목이 없습니다.")

    if user_id == me["id"] and not new_active:
        raise HTTPException(status_code=400, detail="본인 계정은 정지할 수 없습니다.")
    losing_master = cur["role"] == "master" and cur["is_active"] and (new_role != "master" or not new_active)
    if losing_master and _active_master_count(exclude_id=user_id) == 0:
        raise HTTPException(status_code=400, detail="마지막 활성 마스터 계정은 권한 변경/정지할 수 없습니다.")

    updated = supabase.table("users").update(patch).eq("id", user_id).execute().data or []
    if not updated:
        raise HTTPException(status_code=404, detail="사용자를 찾을 수 없습니다.")
    u = updated[0]
    u.pop("password_hash", None)
    return {"user": u}


@app.delete("/api/admin/users/{user_id}")
def admin_delete_user(user_id: int, me: Dict[str, Any] = Depends(require_master)):
    if user_id == me["id"]:
        raise HTTPException(status_code=400, detail="본인 계정은 삭제할 수 없습니다.")
    rows = supabase.table("users").select("id, username, role, is_active").eq("id", user_id).execute().data or []
    if not rows:
        raise HTTPException(status_code=404, detail="사용자를 찾을 수 없습니다.")
    target = rows[0]
    if target["role"] == "master" and target["is_active"] and _active_master_count(exclude_id=user_id) == 0:
        raise HTTPException(status_code=400, detail="마지막 활성 마스터 계정은 삭제할 수 없습니다.")
    supabase.table("users").delete().eq("id", user_id).execute()
    return {"message": f"계정 '{target['username']}' 삭제 완료"}


@app.patch("/api/admin/locations/{loc_id}")
def admin_update_location(loc_id: int, payload: LocationUpdate, _: Dict[str, Any] = Depends(require_master)):
    rows = supabase.table("locations").select("id, name, loc_type, is_active").eq("id", loc_id).execute().data or []
    if not rows:
        raise HTTPException(status_code=404, detail="위치/업체를 찾을 수 없습니다.")
    old = rows[0]

    patch: Dict[str, Any] = {}
    new_name = old["name"]
    if payload.name is not None:
        new_name = payload.name.strip()
        if not new_name:
            raise HTTPException(status_code=400, detail="이름을 입력해 주세요.")
        if new_name != old["name"]:
            dup = supabase.table("locations").select("id").eq("name", new_name).execute().data or []
            if dup:
                raise HTTPException(status_code=409, detail="이미 같은 이름의 위치/업체가 있습니다.")
            patch["name"] = new_name
    if payload.loc_type in ("vendor", "internal"):
        patch["loc_type"] = payload.loc_type
    if payload.is_active is not None:
        patch["is_active"] = payload.is_active
    if not patch:
        raise HTTPException(status_code=400, detail="변경할 항목이 없습니다.")

    updated = supabase.table("locations").update(patch).eq("id", loc_id).execute().data or []
    if "name" in patch:
        # 이름 변경 시 해당 위치를 쓰는 금형/계정도 같이 변경
        supabase.table("molds").update({"current_warehouse": new_name}).eq("current_warehouse", old["name"]).execute()
        supabase.table("users").update({"assigned_location": new_name}).eq("assigned_location", old["name"]).execute()
    return {"location": updated[0] if updated else None}


@app.delete("/api/admin/locations/{loc_id}")
def admin_delete_location(loc_id: int, _: Dict[str, Any] = Depends(require_master)):
    rows = supabase.table("locations").select("id, name").eq("id", loc_id).execute().data or []
    if not rows:
        raise HTTPException(status_code=404, detail="위치/업체를 찾을 수 없습니다.")
    name = rows[0]["name"]
    mold_cnt = _count_rows("molds", "current_warehouse", name)
    if mold_cnt:
        raise HTTPException(
            status_code=409,
            detail=f"'{name}'에 금형 {mold_cnt}건이 있어 삭제할 수 없습니다. 금형을 먼저 다른 곳으로 이동하거나, 삭제 대신 '비활성'으로 바꿔 주세요.",
        )
    user_cnt = _count_rows("users", "assigned_location", name)
    if user_cnt:
        raise HTTPException(
            status_code=409,
            detail=f"'{name}'에 연결된 계정 {user_cnt}개가 있어 삭제할 수 없습니다. 계정의 담당 위치를 먼저 바꾸거나 계정을 삭제해 주세요.",
        )
    supabase.table("locations").delete().eq("id", loc_id).execute()
    return {"message": f"'{name}' 삭제 완료"}


# ------------------ 금형 공통 ------------------

MOLD_LIST_COLS = (
    "id, model_name, model_name2, ajin_no, mold_no, dw_no, part_description, part_count, "
    "current_warehouse, core_status, status_memo, updated_by, updated_at"
)


def _get_mold_or_404(mold_id: int, user: Dict[str, Any], cols: str = "*") -> Dict[str, Any]:
    rows = supabase.table("molds").select(cols).eq("id", mold_id).execute().data or []
    if not rows:
        raise HTTPException(status_code=404, detail="금형을 찾을 수 없습니다.")
    mold = rows[0]
    if user.get("role") == "vendor" and (mold.get("current_warehouse") or "") != (user.get("assigned_location") or ""):
        raise HTTPException(status_code=403, detail="타 보관위치 금형은 열람할 수 없습니다.")
    return mold


def _chunks(seq: List[Any], n: int):
    for i in range(0, len(seq), n):
        yield seq[i : i + n]


# ------------------ 엑셀 업로드 (정보업로드) ------------------

@app.post("/api/molds/upload-excel")
async def upload_excel(
    file: UploadFile = File(...),
    user: Dict[str, Any] = Depends(get_current_user),
):
    if user.get("role") == "vendor":
        raise HTTPException(status_code=403, detail="외부업체 계정은 엑셀 업로드를 할 수 없습니다.")

    fname = (file.filename or "").lower()
    if fname.endswith(".xls"):
        raise HTTPException(
            status_code=400,
            detail="구형 .xls 형식은 지원하지 않습니다. 엑셀에서 '다른 이름으로 저장'으로 .xlsx 로 저장한 뒤 올려주세요.",
        )

    content = await file.read()
    try:
        sheets = parse_mold_workbook(content)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"엑셀 파일을 읽지 못했습니다: {e}")
    if not sheets:
        raise HTTPException(
            status_code=400,
            detail="금형 데이터를 찾지 못했습니다. (AJIN / D.W / TOMYTEC 제목 줄이 있는 LIST OF MOLDS 양식인지 확인하세요)",
        )

    actor = user["initial"]
    ts = now_iso()
    summary: List[Dict[str, Any]] = []

    try:
        # 보관위치 일괄 등록
        wanted = {m["location"] for s in sheets for m in s["molds"] if m["location"]}
        if wanted:
            have = {r["name"] for r in (supabase.table("locations").select("name").execute().data or [])}
            missing = sorted(wanted - have)
            if missing:
                supabase.table("locations").insert(
                    [{"name": n, "loc_type": "vendor", "is_active": True} for n in missing]
                ).execute()

        for s in sheets:
            model = s["model_name"]
            existing = {
                r["mold_key"]: r
                for r in (
                    supabase.table("molds")
                    .select("id, mold_key, current_warehouse")
                    .eq("model_name", model)
                    .limit(5000)
                    .execute()
                    .data
                    or []
                )
            }

            rows = [
                {
                    "model_name": model,
                    "model_name2": s["model_name2"],
                    "ajin_no": m["ajin_no"],
                    "mold_key": m["mold_key"],
                    "mold_no": m["mold_no"],
                    "dw_no": m["dw_no"],
                    "part_description": m["part_description"],
                    "part_count": m["part_count"],
                    "s_dw": m["s_dw"],
                    "s_mold": m["s_mold"],
                    "s_part": m["s_part"],
                    "details": m["details"],
                    "sort_order": m["sort_order"],
                }
                for m in s["molds"]
            ]
            # 현재 보관위치/상태/메모는 payload에 넣지 않아 기존 값이 보존된다
            saved: List[Dict[str, Any]] = []
            for chunk in _chunks(rows, 200):
                saved.extend(
                    supabase.table("molds").upsert(chunk, on_conflict="model_name,mold_key").execute().data or []
                )
            id_by_key = {r["mold_key"]: r["id"] for r in saved}

            new_ids: List[int] = []
            loc_groups: Dict[str, List[int]] = {}
            for m in s["molds"]:
                mid = id_by_key.get(m["mold_key"])
                if not mid:
                    continue
                old = existing.get(m["mold_key"])
                if old is None:
                    new_ids.append(mid)
                if m["location"] and (old is None or not (old.get("current_warehouse") or "")):
                    loc_groups.setdefault(m["location"], []).append(mid)

            for chunk in _chunks(new_ids, 200):
                supabase.table("molds").update({"updated_by": actor, "updated_at": ts}).in_("id", chunk).execute()
            for loc, ids in loc_groups.items():
                for chunk in _chunks(ids, 200):
                    supabase.table("molds").update({"current_warehouse": loc}).in_("id", chunk).execute()

            # 하위품목 전체 교체 (마지막 번호의 하위품목까지 모두 저장)
            all_ids = list(id_by_key.values())
            for chunk in _chunks(all_ids, 100):
                supabase.table("mold_parts").delete().in_("mold_id", chunk).execute()
            part_rows: List[Dict[str, Any]] = []
            for m in s["molds"]:
                mid = id_by_key.get(m["mold_key"])
                if not mid:
                    continue
                for p in m["parts"]:
                    part_rows.append(
                        {
                            "mold_id": mid,
                            "line_no": p["line_no"],
                            "dw_no": p["dw_no"],
                            "mold_no": p["mold_no"],
                            "part_description": p["part_description"],
                            "qty": p["qty"],
                        }
                    )
            for chunk in _chunks(part_rows, 500):
                supabase.table("mold_parts").insert(chunk).execute()

            summary.append(
                {
                    "model_name": model,
                    "model_name2": s["model_name2"],
                    "molds": len(s["molds"]),
                    "new": len(new_ids),
                    "updated": len(s["molds"]) - len(new_ids),
                    "parts": len(part_rows),
                }
            )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"DB 저장 중 오류: {e}")

    lines = [
        f"{x['model_name']} ({x['model_name2']}): 금형 {x['molds']}개 (신규 {x['new']}, 갱신 {x['updated']}), 하위품목 {x['parts']}건"
        for x in summary
    ]
    return {"message": "엑셀 반영 완료\n" + "\n".join(lines), "summary": summary}


@app.delete("/api/admin/models")
def admin_delete_model(name: str = Query(...), _: Dict[str, Any] = Depends(require_master)):
    name = name.strip()
    ids = [r["id"] for r in (supabase.table("molds").select("id").eq("model_name", name).limit(5000).execute().data or [])]
    if not ids:
        raise HTTPException(status_code=404, detail="해당 모델이 없습니다.")
    # 사진 파일도 함께 정리 (실패해도 DB 삭제는 진행)
    try:
        paths: List[str] = []
        for chunk in _chunks(ids, 100):
            for r in supabase.table("mold_photos").select("storage_path, thumb_path").in_("mold_id", chunk).execute().data or []:
                paths += [p for p in (r.get("storage_path"), r.get("thumb_path")) if p]
        for chunk in _chunks(paths, 100):
            supabase.storage.from_(STORAGE_BUCKET).remove(chunk)
    except Exception:
        pass
    supabase.table("molds").delete().eq("model_name", name).execute()
    return {"message": f"모델 '{name}' 및 금형 {len(ids)}개 삭제 완료"}


# ------------------ 모델 목록 · 검색 · 상세 ------------------

@app.get("/api/models")
def list_models(user: Dict[str, Any] = Depends(get_current_user)):
    agg: Dict[str, Dict[str, Any]] = {}
    start = 0
    while True:
        q = supabase.table("molds").select("model_name, model_name2").order("id")
        if user.get("role") == "vendor":
            q = q.eq("current_warehouse", (user.get("assigned_location") or "").strip())
        page = q.range(start, start + 999).execute().data or []
        for r in page:
            a = agg.setdefault(r["model_name"], {"model_name": r["model_name"], "model_name2": r.get("model_name2") or "", "count": 0})
            a["count"] += 1
        if len(page) < 1000:
            break
        start += 1000
    return {"models": sorted(agg.values(), key=lambda x: x["model_name"])}


@app.get("/api/molds")
def search_molds(
    model: List[str] = Query(default=[]),
    ajin: str = Query(default=""),
    mold: str = Query(default=""),
    dw: str = Query(default=""),
    part: str = Query(default=""),
    warehouse: str = Query(default=""),
    q: str = Query(default=""),
    limit: int = Query(default=100, ge=1, le=300),
    offset: int = Query(default=0, ge=0),
    user: Dict[str, Any] = Depends(get_current_user),
):
    query = supabase.table("molds").select(MOLD_LIST_COLS, count="exact")

    # 외부업체 계정은 자사 보관위치 금형만 열람
    if user.get("role") == "vendor":
        assigned = (user.get("assigned_location") or "").strip()
        if not assigned:
            return {"items": [], "total": 0, "offset": offset}
        query = query.eq("current_warehouse", assigned)
    elif warehouse.strip():
        query = query.eq("current_warehouse", warehouse.strip())

    models = [m.strip() for m in model if m and m.strip()]
    if models:
        query = query.in_("model_name", models)

    def like(col: str, text: str):
        nonlocal query
        text = text.strip()
        if text:
            query = query.ilike(col, f"*{text}*")

    like("ajin_no", ajin.strip().lstrip("#"))
    like("s_mold", mold)      # 토미텍 번호 (하위품목 포함)
    like("s_dw", dw)          # 도번 (하위품목 포함)
    like("s_part", part)      # 제품(부품명) (하위품목 포함)

    term = re.sub(r"[,()]", " ", q).strip()
    if term:
        pat = f"*{term}*"
        query = query.or_(
            ",".join(f"{c}.ilike.{pat}" for c in ("ajin_no", "mold_no", "dw_no", "part_description", "s_mold", "s_dw", "s_part"))
        )

    res = query.order("id").range(offset, offset + limit - 1).execute()
    rows = res.data or []
    total = res.count if getattr(res, "count", None) is not None else len(rows)
    return {"items": rows, "total": total, "offset": offset}


@app.get("/api/molds/{mold_id}")
def get_mold_detail(mold_id: int, user: Dict[str, Any] = Depends(get_current_user)):
    mold = _get_mold_or_404(mold_id, user)
    parts = (
        supabase.table("mold_parts").select("line_no, dw_no, mold_no, part_description, qty")
        .eq("mold_id", mold_id).order("line_no").execute().data or []
    )
    photos = (
        supabase.table("mold_photos")
        .select("id, public_url, thumb_url, file_size_kb, uploaded_by, taken_at, uploaded_at")
        .eq("mold_id", mold_id).order("taken_at", desc=True).limit(300).execute().data or []
    )
    memos = (
        supabase.table("mold_memos").select("id, memo, core_status, actor_initial, created_at")
        .eq("mold_id", mold_id).order("created_at", desc=True).limit(100).execute().data or []
    )
    history = (
        supabase.table("transfer_history").select("from_warehouse, to_warehouse, core_status, memo, actor_initial, created_at")
        .eq("mold_id", mold_id).order("created_at", desc=True).limit(30).execute().data or []
    )
    return {"mold": mold, "parts": parts, "photos": photos, "memos": memos, "history": history}


# ------------------ 선택한 금형 일괄 이동 & 메모 ------------------

@app.post("/api/molds/bulk-move")
def bulk_move_molds(payload: BulkMoveRequest, user: Dict[str, Any] = Depends(get_current_user)):
    ids = sorted({int(i) for i in payload.mold_ids})
    if not ids:
        raise HTTPException(status_code=400, detail="이동할 금형을 1개 이상 선택해 주세요.")
    if len(ids) > 300:
        raise HTTPException(status_code=400, detail="한 번에 최대 300개까지 이동할 수 있습니다.")
    to_wh = payload.to_warehouse.strip()
    if not to_wh:
        raise HTTPException(status_code=400, detail="이동할 위치(업체/창고)를 선택해 주세요.")
    if not (supabase.table("locations").select("id").eq("name", to_wh).eq("is_active", True).execute().data or []):
        raise HTTPException(status_code=400, detail="등록되지 않았거나 비활성인 위치입니다.")

    molds = supabase.table("molds").select("id, current_warehouse, core_status").in_("id", ids).execute().data or []
    if not molds:
        raise HTTPException(status_code=404, detail="선택한 금형을 찾을 수 없습니다.")
    if user.get("role") == "vendor":
        assigned = user.get("assigned_location") or ""
        bad = [m["id"] for m in molds if (m.get("current_warehouse") or "") != assigned]
        if bad:
            raise HTTPException(status_code=403, detail="자사 보관 금형이 아닌 항목이 포함되어 있습니다.")

    actor = user["initial"]
    ts = now_iso()
    status = (payload.core_status or "").strip()
    memo = payload.memo.strip()

    upd: Dict[str, Any] = {"current_warehouse": to_wh, "updated_by": actor, "updated_at": ts}
    if status:
        upd["core_status"] = status
    found_ids = [m["id"] for m in molds]
    supabase.table("molds").update(upd).in_("id", found_ids).execute()

    supabase.table("transfer_history").insert(
        [
            {
                "mold_id": m["id"],
                "from_warehouse": m.get("current_warehouse") or "",
                "to_warehouse": to_wh,
                "core_status": status or m.get("core_status") or "",
                "memo": memo,
                "actor_initial": actor,
                "created_at": ts,
            }
            for m in molds
        ]
    ).execute()

    return {"message": f"{len(found_ids)}건 위치 이동 완료 (기록자: {actor})", "moved_ids": found_ids, "actor_initial": actor}


@app.post("/api/molds/{mold_id}/memo")
def save_mold_memo(mold_id: int, payload: MemoUpdateRequest, user: Dict[str, Any] = Depends(get_current_user)):
    mold = _get_mold_or_404(mold_id, user, "id, current_warehouse, core_status")
    memo = payload.memo.strip()
    status = (payload.core_status or "").strip()
    if not memo and not status:
        raise HTTPException(status_code=400, detail="메모 또는 상태를 입력해 주세요.")

    actor = user["initial"]
    ts = now_iso()
    new_status = status or mold.get("core_status") or "정상"

    supabase.table("mold_memos").insert(
        {"mold_id": mold_id, "memo": memo, "core_status": new_status, "actor_initial": actor, "created_at": ts}
    ).execute()
    upd: Dict[str, Any] = {"core_status": new_status, "updated_by": actor, "updated_at": ts}
    if memo:
        upd["status_memo"] = memo
    supabase.table("molds").update(upd).eq("id", mold_id).execute()
    return {"message": f"메모 저장 완료 ({actor})", "updated_by": actor, "updated_at": ts}


# ------------------ 사진 업로드(리사이징+썸네일) ------------------

def compress_photo(raw_bytes: bytes, max_side: int = 1280, quality: int = 72) -> bytes:
    img = Image.open(io.BytesIO(raw_bytes))
    img = ImageOps.exif_transpose(img)
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    w, h = img.size
    if max(w, h) > max_side:
        if w >= h:
            new_w = max_side
            new_h = max(1, int(h * (max_side / w)))
        else:
            new_h = max_side
            new_w = max(1, int(w * (max_side / h)))
        img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)

    out = io.BytesIO()
    img.save(out, format="JPEG", quality=quality, optimize=True)
    return out.getvalue()


def _parse_taken_at(text: Optional[str]) -> Optional[datetime]:
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    if dt.year < 2000 or dt > now + timedelta(days=1):
        return None
    return dt


@app.post("/api/molds/{mold_id}/photos")
async def upload_mold_photo(
    mold_id: int,
    file: UploadFile = File(...),
    taken_at: Optional[str] = Form(default=None),
    user: Dict[str, Any] = Depends(get_current_user),
):
    _get_mold_or_404(mold_id, user, "id, current_warehouse")

    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="빈 이미지 파일입니다.")
    try:
        full = compress_photo(raw, max_side=1280, quality=72)
        thumb = compress_photo(full, max_side=360, quality=62)
    except Exception:
        raise HTTPException(status_code=400, detail="지원되지 않는 이미지 형식입니다.")

    actor = user["initial"]
    now = datetime.now(timezone.utc)
    taken = _parse_taken_at(taken_at) or now
    base = f"mold_{mold_id}/{now.strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(3)}"
    storage_path, thumb_path = f"{base}.jpg", f"{base}_t.jpg"

    try:
        bucket = supabase.storage.from_(STORAGE_BUCKET)
        bucket.upload(path=storage_path, file=full, file_options={"content-type": "image/jpeg", "upsert": "false"})
        bucket.upload(path=thumb_path, file=thumb, file_options={"content-type": "image/jpeg", "upsert": "false"})
        public_url = bucket.get_public_url(storage_path)
        thumb_url = bucket.get_public_url(thumb_path)
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"사진 저장 실패: {e} (Supabase Storage에 '{STORAGE_BUCKET}' 버킷이 Public으로 만들어져 있는지 확인하세요)",
        )

    size_kb = max(1, len(full) // 1024)
    inserted = (
        supabase.table("mold_photos")
        .insert(
            {
                "mold_id": mold_id,
                "storage_path": storage_path,
                "public_url": public_url,
                "thumb_path": thumb_path,
                "thumb_url": thumb_url,
                "file_size_kb": size_kb,
                "uploaded_by": actor,
                "taken_at": taken.isoformat(),
                "uploaded_at": now.isoformat(),
            }
        )
        .execute()
        .data
    )
    return {"message": f"사진 업로드 완료 ({size_kb}KB, {actor})", "photo": inserted[0]}


# ------------------ 무료 플랜 유지용 경량 헬스체크 ------------------

@app.get("/api/health")
def health_ping():
    ts = now_iso()
    supabase.table("health_ping").upsert({"id": 1, "last_ping": ts}).execute()
    return {"status": "ok", "last_ping": ts}