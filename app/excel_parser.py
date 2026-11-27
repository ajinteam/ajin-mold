"""금형 리스트 엑셀 파서 (LIST OF MOLDS 양식)

규칙
- 맨 위 '#TYPE : CPH-302( 10 Series OHANE-PASSENGER CAR)'
    -> 모델명 = 'CPH-302' / 모델명2 = '10 Series OHANE-PASSENGER CAR' (괄호 안)
- AJIN Mold No 칸에 번호가 있는 행 1개 = 금형(코어) 1개
- 번호 칸이 빈 행은 바로 위 금형의 하위품목
- D.W / TOMYTEC / 부품명 / E~(위치 앞)열 상세정보 / TOOL LOCATION(보관위치)
- 합계 줄, 하단 비고 줄은 저장하지 않음 (A~D열이 모두 빈 줄을 만나면 종료)
- 보관위치 칸이 병합되어 있으면 병합 범위 전체에 같은 값을 적용
"""
import datetime
import io
import re
from typing import Any, Dict, List, Optional, Tuple

from openpyxl import load_workbook

TOTAL_WORDS = {"TOTAL", "SUM", "합계", "소계"}


def _clean(v: Any, date_as_code: bool = False) -> str:
    """셀 값을 화면/DB 저장용 문자열로 변환."""
    if v is None:
        return ""
    if isinstance(v, (datetime.datetime, datetime.date)):
        # 엑셀이 '3024-1' 같은 도번을 날짜로 바꿔 놓은 경우 되돌림
        if date_as_code:
            return f"{v.year}-{v.month}"
        return v.strftime("%Y-%m-%d")
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, float):
        if v.is_integer():
            return str(int(v))
        return f"{v:.6f}".rstrip("0").rstrip(".")
    s = str(v).replace("　", " ").replace("\xa0", " ")
    s = s.replace("\r", " ").replace("\n", " ")
    return re.sub(r"\s+", " ", s).strip()


def _merge_map(ws) -> Dict[Tuple[int, int], Tuple[int, int]]:
    mp: Dict[Tuple[int, int], Tuple[int, int]] = {}
    for rng in ws.merged_cells.ranges:
        for r in range(rng.min_row, rng.max_row + 1):
            for c in range(rng.min_col, rng.max_col + 1):
                mp[(r, c)] = (rng.min_row, rng.min_col)
    return mp


def _val(ws, mp, r: int, c: int, fill_merged: bool = False) -> Any:
    v = ws.cell(r, c).value
    if v is None and fill_merged and (r, c) in mp:
        tr, tc = mp[(r, c)]
        v = ws.cell(tr, tc).value
    return v


def _parse_model(ws) -> Tuple[str, str]:
    text = ""
    for r in range(1, min(12, ws.max_row) + 1):
        for c in range(1, min(12, ws.max_column) + 1):
            raw = _clean(ws.cell(r, c).value)
            if "TYPE" in raw.upper() and (":" in raw or "：" in raw):
                text = re.split(r"[:：]", raw, maxsplit=1)[1].strip()
                break
        if text:
            break
    if not text:
        return (ws.title.strip() or "UNKNOWN-MODEL"), ""
    m = re.match(r"^(.*?)\s*[\(（](.*)[\)）]\s*$", text)
    if m and m.group(1).strip():
        return m.group(1).strip(), m.group(2).strip()
    return text, ""


def _find_header(ws) -> Tuple[Optional[int], Dict[str, int]]:
    for r in range(1, min(20, ws.max_row) + 1):
        texts = {c: _clean(ws.cell(r, c).value).upper() for c in range(1, ws.max_column + 1)}
        joined = " ".join(texts.values())
        if "AJIN" in joined and ("TOMY" in joined or "LOCATION" in joined):
            cols: Dict[str, int] = {}
            for c, up in texts.items():
                if not up:
                    continue
                if "AJIN" in up and "ajin" not in cols:
                    cols["ajin"] = c
                elif ("D.W" in up or up == "DW") and "dw" not in cols:
                    cols["dw"] = c
                elif "TOMY" in up and "mold" not in cols:
                    cols["mold"] = c
                elif "PART" in up and "part" not in cols:
                    cols["part"] = c
                elif "LOCATION" in up and "loc" not in cols:
                    cols["loc"] = c
            return r, cols
    return None, {}


def _detail_layout(ws, header_row: int, cols: List[int]) -> List[Dict[str, Any]]:
    """상세정보 열 정의. 크기(W x L x H)는 한 항목으로 합친다."""
    layout: List[Dict[str, Any]] = []
    i = 0
    while i < len(cols):
        c = cols[i]
        top = _clean(ws.cell(header_row, c).value)
        sub = _clean(ws.cell(header_row + 1, c).value)
        if "SIZE" in top.upper():
            # SIZE 열부터 W / x / L / x / H 로 이어지는 열을 묶음
            group = [c]
            j = i + 1
            while j < len(cols) and _clean(ws.cell(header_row + 1, cols[j]).value).upper() in ("W", "L", "H", "X", ""):
                if _clean(ws.cell(header_row, cols[j]).value):
                    break
                group.append(cols[j])
                j += 1
            dims = [g for g in group if _clean(ws.cell(header_row + 1, g).value).upper() in ("W", "L", "H")]
            layout.append({"label": f"{top} (W x L x H)", "cols": dims or [c], "join": " x "})
            i = j
            continue
        label = top
        if sub and sub != top:
            label = f"{top} {sub}".strip()
        if label:
            layout.append({"label": label, "cols": [c], "join": ""})
        i += 1
    return layout


def parse_sheet(ws) -> Optional[Dict[str, Any]]:
    header_row, cols = _find_header(ws)
    if not header_row:
        return None

    ajin_c = cols.get("ajin", 1)
    dw_c = cols.get("dw", 2)
    mold_c = cols.get("mold", 3)
    part_c = cols.get("part", 4)
    loc_c = cols.get("loc", ws.max_column + 1)

    # 헤더가 두 줄(AJIN / Mold No)인 양식 처리
    nxt = " ".join(_clean(ws.cell(header_row + 1, c).value).upper() for c in range(1, ws.max_column + 1))
    start_row = header_row + 2 if ("MOLD" in nxt or "NO" in nxt.split()) else header_row + 1

    detail_cols = list(range(part_c + 1, loc_c))
    layout = _detail_layout(ws, header_row, detail_cols)
    qty_cols = [d["cols"][0] for d in layout if re.search(r"CA.?TY|QTY|수량", d["label"], re.I)]
    qty_c = qty_cols[0] if qty_cols else None

    model_name, model_name2 = _parse_model(ws)
    mp = _merge_map(ws)

    molds: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None

    for r in range(start_row, ws.max_row + 1):
        a = _clean(_val(ws, mp, r, ajin_c))
        b = _clean(_val(ws, mp, r, dw_c), date_as_code=True)
        c = _clean(_val(ws, mp, r, mold_c))
        d = _clean(_val(ws, mp, r, part_c))

        if not any([a, b, c, d]):
            break  # 합계/비고 줄 이전에서 종료
        if d.upper() in TOTAL_WORDS or a.upper() in TOTAL_WORDS:
            break

        qty = _clean(_val(ws, mp, r, qty_c)) if qty_c else ""
        part = {"dw_no": b, "mold_no": c, "part_description": d, "qty": qty}

        if a or current is None:
            loc = _clean(_val(ws, mp, r, loc_c, fill_merged=True)) if loc_c <= ws.max_column else ""
            details = []
            for item in layout:
                vals = [_clean(_val(ws, mp, r, cc)) for cc in item["cols"]]
                vals = [v for v in vals if v and v.lower() != "x"]
                if vals:
                    details.append({"label": item["label"], "value": item["join"].join(vals) if item["join"] else vals[0]})
            current = {
                "ajin_no": a,
                "mold_no": c,
                "dw_no": b,
                "part_description": d,
                "location": loc,
                "details": details,
                "parts": [],
                "source_row": r,
            }
            molds.append(current)
        current["parts"].append(part)

    # 번호 순서 그대로 정렬번호 / 모델 내 고유키 / 검색용 문자열
    used = set()
    for idx, m in enumerate(molds):
        m["sort_order"] = idx
        base = m["mold_no"] or f"AJIN:{m['ajin_no']}"
        key, n = base, 2
        while key in used:
            key = f"{base}#{n}"
            n += 1
        used.add(key)
        m["mold_key"] = key
        for i, p in enumerate(m["parts"], start=1):
            p["line_no"] = i

        def uniq(field: str) -> str:
            seen: List[str] = []
            for p in m["parts"]:
                v = p[field]
                if v and v not in seen:
                    seen.append(v)
            return " | ".join(seen)

        m["s_dw"] = uniq("dw_no")
        m["s_mold"] = uniq("mold_no")
        m["s_part"] = uniq("part_description")
        m["part_count"] = len(m["parts"])

    return {"model_name": model_name, "model_name2": model_name2, "sheet": ws.title, "molds": molds}


def parse_mold_workbook(file_bytes: bytes) -> List[Dict[str, Any]]:
    wb = load_workbook(filename=io.BytesIO(file_bytes), data_only=True)
    out: List[Dict[str, Any]] = []
    for ws in wb.worksheets:
        res = parse_sheet(ws)
        if res and res["molds"]:
            out.append(res)
    return out
