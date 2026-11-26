import hashlib
import io
import os
import secrets
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import pandas as pd
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from openpyxl import load_workbook
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
templates = Jinja2Templates(directory="templates")

# 메모리 기반 간단 세션 토큰 저장소 (단일 인스턴스 기준, 필요시 DB/Redis로 확장 가능)
SESSIONS: Dict[str, Dict[str, Any]] = {}


def hash_password(raw_pw: str) -> str:
    return hashlib.sha256(raw_pw.encode("utf-8")).hexdigest()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_current_user(authorization: Optional[str] = Header(default=None)) -> Dict[str, Any]:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="로그인이 필요합니다.")
    token = authorization.split(" ", 1)[1].strip()
    session = SESSIONS.get(token)
    if not session:
        raise HTTPException(status_code=401, detail="세션이 만료되었습니다. 다시 로그인해 주세요.")
    return session


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


class BulkMoveRequest(BaseModel):
    mold_ids: List[int]
    to_warehouse: str
    core_status: Optional[str] = None
    memo: str = ""


class MemoUpdateRequest(BaseModel):
    memo: str
    core_status: Optional[str] = None


# ------------------ 페이지 & 인증 ------------------

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


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

    token = secrets.token_urlsafe(32)
    user_info = {
        "id": u["id"],
        "username": u["username"],
        "full_name": u["full_name"],
        "initial": u["initial"],
        "role": u["role"],
        "assigned_location": u.get("assigned_location"),
    }
    SESSIONS[token] = user_info
    return {"token": token, "user": user_info}


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


@app.patch("/api/admin/users/{user_id}")
def admin_update_user(user_id: int, payload: UserUpdate, _: Dict[str, Any] = Depends(require_master)):
    patch: Dict[str, Any] = {}
    if payload.full_name is not None:
        patch["full_name"] = payload.full_name.strip()
    if payload.initial is not None and payload.initial.strip():
        patch["initial"] = payload.initial.strip().upper()
    if payload.role in ("master", "staff", "vendor"):
        patch["role"] = payload.role
    if payload.assigned_location is not None:
        patch["assigned_location"] = payload.assigned_location.strip() or None
    if payload.is_active is not None:
        patch["is_active"] = payload.is_active
    if payload.new_password:
        patch["password_hash"] = hash_password(payload.new_password)

    if not patch:
        raise HTTPException(status_code=400, detail="변경할 항목이 없습니다.")

    updated = supabase.table("users").update(patch).eq("id", user_id).execute().data or []
    if not updated:
        raise HTTPException(status_code=404, detail="사용자를 찾을 수 없습니다.")
    u = updated[0]
    u.pop("password_hash", None)
    return {"user": u}


# ------------------ 엑셀 파싱 & 업로드 (기존 양식 호환) ------------------

def parse_mold_excel(file_bytes: bytes) -> List[Dict[str, str]]:
    wb = load_workbook(filename=io.BytesIO(file_bytes), data_only=True)
    ws = wb.active

    # 병합 셀 값 채우기
    for rng in list(ws.merged_cells.ranges):
        top_val = ws.cell(rng.min_row, rng.min_col).value
        for r in range(rng.min_row, rng.max_row + 1):
            for c in range(rng.min_col, rng.max_col + 1):
                if ws.cell(r, c).value is None:
                    ws.cell(r, c).value = top_val

    model_name = ""
    for r in range(1, min(8, ws.max_row + 1)):
        for c in range(1, min(10, ws.max_column + 1)):
            v = str(ws.cell(r, c).value or "").strip()
            if "#TYPE" in v.upper():
                parts = v.split(":", 1)
                model_name = parts[1].strip() if len(parts) > 1 else v
                break
        if model_name:
            break
    if not model_name:
        model_name = ws.title.strip() or "UNKNOWN-MODEL"

    # 헤더 행 탐색 ('AJIN'과 'TOMYTEC' 또는 'LOCATION' 키워드)
    header_row = None
    col_map: Dict[str, int] = {}
    for r in range(1, min(15, ws.max_row + 1)):
        row_vals = [str(ws.cell(r, c).value or "").strip() for c in range(1, ws.max_column + 1)]
        joined = " ".join(row_vals).upper()
        if "AJIN" in joined and ("TOMYTEC" in joined or "LOCATION" in joined):
            header_row = r
            for idx, val in enumerate(row_vals, start=1):
                up = val.upper()
                if "AJIN" in up:
                    col_map["ajin"] = idx
                elif "D.W" in up or up == "DW":
                    col_map["dw"] = idx
                elif "TOMYTEC" in up or "MOLD" in up:
                    if "mold" not in col_map:
                        col_map["mold"] = idx
                elif "LOCATION" in up:
                    col_map["location"] = idx
            break

    start_row = (header_row + 2) if header_row else 7
    ajin_col = col_map.get("ajin", 1)
    dw_col = col_map.get("dw", 2)
    mold_col = col_map.get("mold", 3)
    part_col = 4
    loc_col = col_map.get("location", 17)

    last_ajin = ""
    last_mold = ""
    parsed: List[Dict[str, str]] = []

    for r in range(start_row, ws.max_row + 1):
        raw_ajin = str(ws.cell(r, ajin_col).value or "").strip()
        raw_dw = str(ws.cell(r, dw_col).value or "").strip()
        raw_mold = str(ws.cell(r, mold_col).value or "").strip()
        raw_part = str(ws.cell(r, part_col).value or "").strip()
        raw_loc = str(ws.cell(r, loc_col).value or "").strip()

        if raw_ajin and raw_ajin.lower() != "nan":
            last_ajin = raw_ajin.replace(".0", "")
        if raw_mold and raw_mold.lower() != "nan":
            last_mold = raw_mold

        if not any([raw_dw, raw_part, raw_mold, raw_ajin]):
            continue
        if raw_part.upper() in ("TOTAL", "SUM", "합계"):
            continue

        parsed.append(
            {
                "model_name": model_name,
                "ajin_no": last_ajin or "-",
                "mold_no": last_mold or "-",
                "dw_no": raw_dw,
                "part_description": raw_part,
                "current_warehouse": raw_loc,
            }
        )
    return parsed


@app.post("/api/molds/upload-excel")
async def upload_excel(
    file: UploadFile = File(...),
    user: Dict[str, Any] = Depends(get_current_user),
):
    if user.get("role") == "vendor":
        raise HTTPException(status_code=403, detail="외부업체 계정은 엑셀 일괄 업로드를 할 수 없습니다.")

    content = await file.read()
    items = parse_mold_excel(content)
    if not items:
        raise HTTPException(status_code=400, detail="업로드할 금형 데이터를 찾지 못했습니다.")

    actor = user["initial"]
    ts = now_iso()
    inserted_count = 0
    updated_count = 0

    for item in items:
        # 기존 보관위치 자동 등록
        loc_name = item["current_warehouse"].strip()
        if loc_name:
            loc_res = supabase.table("locations").select("id").eq("name", loc_name).execute().data or []
            if not loc_res:
                supabase.table("locations").insert({"name": loc_name, "loc_type": "vendor", "is_active": True}).execute()

        existing = (
            supabase.table("operational_molds")
            .select("id, current_warehouse, core_status")
            .eq("model_name", item["model_name"])
            .eq("ajin_no", item["ajin_no"])
            .eq("mold_no", item["mold_no"])
            .eq("dw_no", item["dw_no"])
            .eq("part_description", item["part_description"])
            .execute()
            .data
            or []
        )

        if existing:
            mold_id = existing[0]["id"]
            # 기존 현장 위치/상태는 보존하되 비어있을 때만 엑셀 위치 반영
            patch: Dict[str, Any] = {"updated_by": actor, "updated_at": ts}
            if not existing[0].get("current_warehouse") and loc_name:
                patch["current_warehouse"] = loc_name
            supabase.table("operational_molds").update(patch).eq("id", mold_id).execute()
            updated_count += 1
        else:
            supabase.table("operational_molds").insert(
                {
                    "model_name": item["model_name"],
                    "ajin_no": item["ajin_no"],
                    "mold_no": item["mold_no"],
                    "dw_no": item["dw_no"],
                    "part_description": item["part_description"],
                    "current_warehouse": loc_name,
                    "core_status": "정상",
                    "status_memo": "",
                    "updated_by": actor,
                    "updated_at": ts,
                }
            ).execute()
            inserted_count += 1

    return {
        "message": f"엑셀 반영 완료 (신규 {inserted_count}건, 기존 갱신 {updated_count}건)",
        "model_name": items[0]["model_name"],
        "total_parsed": len(items),
    }


# ------------------ 검색 · 조회 · QR 스캔 lookup ------------------

@app.get("/api/molds")
def search_molds(
    q: str = Query(default=""),
    warehouse: str = Query(default=""),
    limit: int = Query(default=30, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    user: Dict[str, Any] = Depends(get_current_user),
):
    query = supabase.table("operational_molds").select(
        "id, model_name, ajin_no, mold_no, dw_no, part_description, current_warehouse, core_status, status_memo, updated_by, updated_at"
    )

    # 외부업체 계정은 자사 보관위치 금형만 열람
    if user.get("role") == "vendor":
        assigned_loc = (user.get("assigned_location") or "").strip()
        if not assigned_loc:
            return {"items": [], "count": 0}
        query = query.eq("current_warehouse", assigned_loc)
    elif warehouse.strip():
        query = query.eq("current_warehouse", warehouse.strip())

    term = q.strip()
    if term:
        # 모델명, 아진No, 몰드No, 품명, D.W No 통합 검색
        pattern = f"*{term}*"
        query = query.or_(
            f"model_name.ilike.{pattern},"
            f"ajin_no.ilike.{pattern},"
            f"mold_no.ilike.{pattern},"
            f"part_description.ilike.{pattern},"
            f"dw_no.ilike.{pattern}"
        )

    rows = query.order("id", desc=False).range(offset, offset + limit - 1).execute().data or []
    return {"items": rows, "count": len(rows)}


@app.get("/api/molds/{mold_id}")
def get_mold_detail(mold_id: int, user: Dict[str, Any] = Depends(get_current_user)):
    rows = supabase.table("operational_molds").select("*").eq("id", mold_id).execute().data or []
    if not rows:
        raise HTTPException(status_code=404, detail="금형을 찾을 수 없습니다.")
    mold = rows[0]

    if user.get("role") == "vendor":
        if mold.get("current_warehouse") != user.get("assigned_location"):
            raise HTTPException(status_code=403, detail="타 보관위치 금형은 열람할 수 없습니다.")

    history = (
        supabase.table("transfer_history")
        .select("*")
        .eq("mold_id", mold_id)
        .order("created_at", desc=True)
        .limit(20)
        .execute()
        .data
        or []
    )
    photos = (
        supabase.table("mold_photos")
        .select("*")
        .eq("mold_id", mold_id)
        .order("uploaded_at", desc=True)
        .execute()
        .data
        or []
    )
    return {"mold": mold, "history": history, "photos": photos}


# ------------------ QR 다중 스캔 일괄 이동 & 메모 ------------------

@app.post("/api/molds/bulk-move")
def bulk_move_molds(payload: BulkMoveRequest, user: Dict[str, Any] = Depends(get_current_user)):
    if not payload.mold_ids:
        raise HTTPException(status_code=400, detail="이동할 금형을 1개 이상 선택해 주세요.")
    to_wh = payload.to_warehouse.strip()
    if not to_wh:
        raise HTTPException(status_code=400, detail="이동할 위치(업체/창고)를 선택해 주세요.")

    actor = user["initial"]
    ts = now_iso()
    moved_ids: List[int] = []

    for mid in payload.mold_ids:
        rows = supabase.table("operational_molds").select("*").eq("id", mid).execute().data or []
        if not rows:
            continue
        mold = rows[0]
        from_wh = mold.get("current_warehouse") or ""

        if user.get("role") == "vendor" and from_wh != user.get("assigned_location"):
            raise HTTPException(status_code=403, detail=f"금형 ID {mid}는 자사 보유 금형이 아닙니다.")

        update_data: Dict[str, Any] = {
            "current_warehouse": to_wh,
            "updated_by": actor,
            "updated_at": ts,
        }
        if payload.core_status:
            update_data["core_status"] = payload.core_status.strip()
        if payload.memo.strip():
            update_data["status_memo"] = payload.memo.strip()

        supabase.table("operational_molds").update(update_data).eq("id", mid).execute()

        supabase.table("transfer_history").insert(
            {
                "mold_id": mid,
                "from_warehouse": from_wh,
                "to_warehouse": to_wh,
                "core_status": update_data.get("core_status") or mold.get("core_status") or "정상",
                "memo": payload.memo.strip(),
                "actor_initial": actor,
                "created_at": ts,
            }
        ).execute()
        moved_ids.append(mid)

    return {
        "message": f"{len(moved_ids)}건 위치 이동 완료 (기록자: {actor})",
        "moved_ids": moved_ids,
        "actor_initial": actor,
    }


@app.post("/api/molds/{mold_id}/memo")
def update_mold_memo(mold_id: int, payload: MemoUpdateRequest, user: Dict[str, Any] = Depends(get_current_user)):
    rows = supabase.table("operational_molds").select("*").eq("id", mold_id).execute().data or []
    if not rows:
        raise HTTPException(status_code=404, detail="금형을 찾을 수 없습니다.")
    mold = rows[0]
    if user.get("role") == "vendor" and mold.get("current_warehouse") != user.get("assigned_location"):
        raise HTTPException(status_code=403, detail="권한이 없습니다.")

    actor = user["initial"]
    ts = now_iso()
    memo_text = payload.memo.strip()
    new_status = (payload.core_status or mold.get("core_status") or "정상").strip()

    supabase.table("operational_molds").update(
        {
            "status_memo": memo_text,
            "core_status": new_status,
            "updated_by": actor,
            "updated_at": ts,
        }
    ).eq("id", mold_id).execute()

    supabase.table("transfer_history").insert(
        {
            "mold_id": mold_id,
            "from_warehouse": mold.get("current_warehouse") or "",
            "to_warehouse": mold.get("current_warehouse") or "",
            "core_status": new_status,
            "memo": f"[메모/상태] {memo_text}",
            "actor_initial": actor,
            "created_at": ts,
        }
    ).execute()

    return {"message": f"메모 저장 완료 ({actor})", "updated_by": actor, "updated_at": ts}


# ------------------ 사진 최적화 업로드(리사이징+압축) & 날짜순 조회 ------------------

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


@app.post("/api/molds/{mold_id}/photos")
async def upload_mold_photo(
    mold_id: int,
    file: UploadFile = File(...),
    user: Dict[str, Any] = Depends(get_current_user),
):
    rows = supabase.table("operational_molds").select("id, model_name, ajin_no, mold_no, current_warehouse").eq("id", mold_id).execute().data or []
    if not rows:
        raise HTTPException(status_code=404, detail="금형을 찾을 수 없습니다.")
    mold = rows[0]
    if user.get("role") == "vendor" and mold.get("current_warehouse") != user.get("assigned_location"):
        raise HTTPException(status_code=403, detail="권한이 없습니다.")

    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="빈 이미지 파일입니다.")

    try:
        optimized = compress_photo(raw, max_side=1280, quality=72)
    except Exception:
        raise HTTPException(status_code=400, detail="지원되지 않는 이미지 형식입니다.")

    actor = user["initial"]
    ts = now_iso()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    safe_label = f"{mold['model_name']}_{mold['ajin_no']}_{mold['mold_no']}".replace("/", "_").replace(" ", "_")
    storage_path = f"mold_{mold_id}/{stamp}_{actor}_{safe_label}_{secrets.token_hex(3)}.jpg"

    supabase.storage.from_(STORAGE_BUCKET).upload(
        path=storage_path,
        file=optimized,
        file_options={"content-type": "image/jpeg", "upsert": "false"},
    )
    public_url = supabase.storage.from_(STORAGE_BUCKET).get_public_url(storage_path)
    size_kb = max(1, len(optimized) // 1024)

    inserted = (
        supabase.table("mold_photos")
        .insert(
            {
                "mold_id": mold_id,
                "storage_path": storage_path,
                "public_url": public_url,
                "file_size_kb": size_kb,
                "uploaded_by": actor,
                "uploaded_at": ts,
            }
        )
        .execute()
        .data
    )

    return {
        "message": f"사진 최적화 업로드 완료 ({size_kb}KB, {actor})",
        "photo": inserted[0],
    }


# ------------------ 무료 플랜 유지용 경량 헬스체크 ------------------

@app.get("/api/health")
def health_ping():
    ts = now_iso()
    supabase.table("health_ping").upsert({"id": 1, "last_ping": ts}).execute()
    return {"status": "ok", "last_ping": ts}