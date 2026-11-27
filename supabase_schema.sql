-- 금형 QR · 위치 · 사진 관리 — 전체 스키마 (새로 만들 때 사용)

-- 1. 보관 위치 / 외부 업체 마스터 테이블
CREATE TABLE IF NOT EXISTS locations (
    id BIGSERIAL PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,          -- 예: '아진본사', '오리엔트', '광신플라텍'
    loc_type TEXT NOT NULL DEFAULT 'vendor', -- 'internal' | 'vendor'
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 2. 사용자(사내 관리자/작업자 + 외부 업체) 계정 테이블
CREATE TABLE IF NOT EXISTS users (
    id BIGSERIAL PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    full_name TEXT NOT NULL,
    initial TEXT NOT NULL,               -- 예: 'JS', 'MY', 'MS', 'OR'
    role TEXT NOT NULL DEFAULT 'staff',  -- 'master' | 'staff' | 'vendor'
    assigned_location TEXT,              -- role='vendor' 일 때 접근 가능한 보관위치(업체명)
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 3. 금형 마스터 (아진 번호 1개 = 금형/코어 1개)
CREATE TABLE IF NOT EXISTS molds (
    id BIGSERIAL PRIMARY KEY,
    model_name TEXT NOT NULL,                 -- 모델명1 예: 'CPH-302'
    model_name2 TEXT NOT NULL DEFAULT '',     -- 모델명2 예: '10 Series OHANE-PASSENGER CAR'
    ajin_no TEXT NOT NULL DEFAULT '',         -- 아진 번호 예: '1', '1-.1' (화면에는 #1 로 표시)
    mold_key TEXT NOT NULL,                   -- 모델 안에서 고유한 키 (토미텍 번호, 없으면 'AJIN:번호')
    mold_no TEXT NOT NULL DEFAULT '',         -- 토미텍(MOLD) 번호
    dw_no TEXT NOT NULL DEFAULT '',           -- 도번 (D.W No)
    part_description TEXT NOT NULL DEFAULT '',-- 부품 설명 (대표 품목)
    part_count INT NOT NULL DEFAULT 1,        -- 하위품목 수 (대표 품목 포함)
    s_dw TEXT NOT NULL DEFAULT '',            -- 검색용: 모든 하위품목의 도번
    s_mold TEXT NOT NULL DEFAULT '',          -- 검색용: 모든 하위품목의 토미텍 번호
    s_part TEXT NOT NULL DEFAULT '',          -- 검색용: 모든 하위품목의 부품명
    details JSONB NOT NULL DEFAULT '[]'::jsonb, -- 엑셀 E~P열 상세정보 [{"label":..,"value":..}]
    sort_order INT NOT NULL DEFAULT 0,
    current_warehouse TEXT NOT NULL DEFAULT '',
    core_status TEXT NOT NULL DEFAULT '정상',
    status_memo TEXT NOT NULL DEFAULT '',     -- 최근 메모
    updated_by TEXT NOT NULL DEFAULT '',      -- 최종 변경자(이니셜)
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (model_name, mold_key)
);

CREATE INDEX IF NOT EXISTS idx_molds_model ON molds (model_name);
CREATE INDEX IF NOT EXISTS idx_molds_warehouse ON molds (current_warehouse);
CREATE INDEX IF NOT EXISTS idx_molds_ajin ON molds (ajin_no);

-- 4. 하위품목 (금형 1개에 속한 품목들, 대표 품목이 1번)
CREATE TABLE IF NOT EXISTS mold_parts (
    id BIGSERIAL PRIMARY KEY,
    mold_id BIGINT NOT NULL REFERENCES molds(id) ON DELETE CASCADE,
    line_no INT NOT NULL DEFAULT 1,
    dw_no TEXT NOT NULL DEFAULT '',
    mold_no TEXT NOT NULL DEFAULT '',
    part_description TEXT NOT NULL DEFAULT '',
    qty TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_parts_mold ON mold_parts (mold_id, line_no);

-- 5. 이동 이력
CREATE TABLE IF NOT EXISTS transfer_history (
    id BIGSERIAL PRIMARY KEY,
    mold_id BIGINT NOT NULL REFERENCES molds(id) ON DELETE CASCADE,
    from_warehouse TEXT DEFAULT '',
    to_warehouse TEXT DEFAULT '',
    core_status TEXT DEFAULT '',
    memo TEXT DEFAULT '',
    actor_initial TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_history_mold_created ON transfer_history (mold_id, created_at DESC);

-- 6. 금형 상태 메모 (저장할 때마다 한 줄씩 쌓임)
CREATE TABLE IF NOT EXISTS mold_memos (
    id BIGSERIAL PRIMARY KEY,
    mold_id BIGINT NOT NULL REFERENCES molds(id) ON DELETE CASCADE,
    memo TEXT NOT NULL DEFAULT '',
    core_status TEXT NOT NULL DEFAULT '',
    actor_initial TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_memos_mold_created ON mold_memos (mold_id, created_at DESC);

-- 7. 금형 사진 메타데이터 (실제 파일은 Supabase Storage 'mold-photos' 버킷, 썸네일 포함)
CREATE TABLE IF NOT EXISTS mold_photos (
    id BIGSERIAL PRIMARY KEY,
    mold_id BIGINT NOT NULL REFERENCES molds(id) ON DELETE CASCADE,
    storage_path TEXT NOT NULL,
    public_url TEXT NOT NULL,
    thumb_path TEXT NOT NULL DEFAULT '',
    thumb_url TEXT NOT NULL DEFAULT '',
    file_size_kb INT NOT NULL DEFAULT 0,
    uploaded_by TEXT NOT NULL,
    taken_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),   -- 촬영일
    uploaded_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_photos_mold_taken ON mold_photos (mold_id, taken_at DESC);

-- 8. 무료 플랜 일시정지 알림 방지용 경량 핑 테이블
CREATE TABLE IF NOT EXISTS health_ping (
    id INT PRIMARY KEY DEFAULT 1,
    last_ping TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO health_ping (id) VALUES (1) ON CONFLICT (id) DO NOTHING;

-- 기본 위치 및 마스터 계정 시드 (초기 비밀번호는 로그인 후 변경 권장)
INSERT INTO locations (name, loc_type) VALUES
    ('아진본사', 'internal'),
    ('대천공장', 'internal'),
    ('오리엔트', 'vendor'),
    ('광신플라텍', 'vendor')
ON CONFLICT (name) DO NOTHING;

-- 초기 마스터 계정: 아이디 admin / 비번 1234 / 이니셜 JS
-- 아래 해시는 SHA-256('1234') 예시이며, 앱 코드에서도 동일 해시 방식을 사용합니다.
INSERT INTO users (username, password_hash, full_name, initial, role, assigned_location) VALUES
    ('admin', '03ac674216f3e15c761ee1a5e255f067953623c8b388b4459e13f978d7c846f4', '마스터관리자', 'JS', 'master', NULL)
ON CONFLICT (username) DO NOTHING;