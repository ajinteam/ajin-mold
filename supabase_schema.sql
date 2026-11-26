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

-- 3. 금형 마스터 및 현재 상태 테이블
CREATE TABLE IF NOT EXISTS operational_molds (
    id BIGSERIAL PRIMARY KEY,
    model_name TEXT NOT NULL,            -- 예: 'CPH-301E (OSHI17)'
    ajin_no TEXT NOT NULL,               -- 예: '2'
    mold_no TEXT NOT NULL,               -- 예: '796099-P02'
    dw_no TEXT DEFAULT '',               -- 예: '301E 102'
    part_description TEXT DEFAULT '',    -- 예: 'SIDE W/ GLASS'
    current_warehouse TEXT DEFAULT '',   -- 현재 보관위치 / 업체명
    core_status TEXT DEFAULT '정상',
    status_memo TEXT DEFAULT '',
    updated_by TEXT DEFAULT '',          -- 로그인 이니셜
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (model_name, ajin_no, mold_no, dw_no, part_description)
);

CREATE INDEX IF NOT EXISTS idx_molds_search
    ON operational_molds (model_name, ajin_no, mold_no, part_description);
CREATE INDEX IF NOT EXISTS idx_molds_warehouse
    ON operational_molds (current_warehouse);

-- 4. 이동 / 상태 / 메모 변경 이력 테이블
CREATE TABLE IF NOT EXISTS transfer_history (
    id BIGSERIAL PRIMARY KEY,
    mold_id BIGINT NOT NULL REFERENCES operational_molds(id) ON DELETE CASCADE,
    from_warehouse TEXT DEFAULT '',
    to_warehouse TEXT DEFAULT '',
    core_status TEXT DEFAULT '',
    memo TEXT DEFAULT '',
    actor_initial TEXT NOT NULL,         -- 작업자 이니셜
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_history_mold_created
    ON transfer_history (mold_id, created_at DESC);

-- 5. 금형 사진 메타데이터 테이블 (실제 파일은 Supabase Storage에 저장)
CREATE TABLE IF NOT EXISTS mold_photos (
    id BIGSERIAL PRIMARY KEY,
    mold_id BIGINT NOT NULL REFERENCES operational_molds(id) ON DELETE CASCADE,
    storage_path TEXT NOT NULL,
    public_url TEXT NOT NULL,
    file_size_kb INT NOT NULL DEFAULT 0,
    uploaded_by TEXT NOT NULL,           -- 업로더 이니셜
    uploaded_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_photos_mold_date
    ON mold_photos (mold_id, uploaded_at DESC);

-- 6. 무료 플랜 일시정지 알림 방지용 경량 핑 테이블
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