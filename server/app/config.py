"""앱 서버 설정 — `app.env` 를 읽고, 이미 설정된 환경변수가 이긴다.

파일 경로는 `APP_ENV_FILE`(없으면 사용자 설정 디렉터리의 `vhl-app/app.env`)이다.
값은 이 파일·환경변수에만 있다. 코드·문서에 호스트·포트·키를 적지 않는다 (CLAUDE.md §보안).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

APP_API = "1.0.6"


def default_env_file() -> Path:
    return Path(os.environ.get("APP_ENV_FILE") or "~/.config/vhl-app/app.env").expanduser()


def load_env_file(path: Optional[Path] = None) -> Optional[Path]:
    """`KEY=VALUE` 줄을 **아직 없는** 환경변수에만 넣는다. 빈 값은 넣지 않는다."""
    p = Path(path) if path else default_env_file()
    if not p.is_file():
        return None
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if v and k not in os.environ:
            os.environ[k] = v
    return p


def _flag(name: str, default: str = "0") -> bool:
    return (os.environ.get(name) or default).strip() in ("1", "true", "yes")


@dataclass
class Settings:
    data_dir: Path
    mock: bool
    a5000_url: str
    a5000_key: str
    inpaint_port: int
    shared_token: str
    bind_host: str
    bind_port: int
    render_mode: str          # "camera"(계약 D8) | "view"(A1 전 임시: view 생략 → A5000 이 시점 선택)
    mock_time_scale: float    # 가짜 모드 단계 시간 배율(시험용 0)
    mock_source: str          # 가짜 모드 원천 자산(앱 저장소의 asset_id)
    timeout_s: float = 600.0  # 실행 상태 10분
    review3d_s: float = 1800.0  # 3D 확인 30분
    max_attempts: int = 3
    facehint: bool = True  # S14 APP_FACEHINT: 상세화에 "사람·캐릭터·동물이면 얼굴이 보이게" 규칙
    regionrefine: bool = True  # S13 APP_REGIONREFINE: 보정·분류에 라쏘 이미지·mask_ratio, selected_part·region_fit
    inpaintprompt: bool = True  # S13b APP_INPAINTPROMPT: 인페인팅에 보정의 inpaint_prompt(장면 문장은 자산용 그대로)
    rejectraw: bool = True  # S13 APP_REJECTRAW: prompt 재시도 보정에 사용자 거절 원문도
    worker_delete: bool = False  # APP_WORKER_DELETE: 자산 삭제 때 A5000 워커 저장소도 지움(기본 0 = 보관, 사용자 결정 11-12)
    relphrase: bool = True  # S12 APP_RELPHRASE: 보정 문장에서 바뀐 부위를 대상 뒤 관계어로(끔 = S4–S11 보정 템플릿)
    realstyle: bool = True  # S11 APP_REALSTYLE: 기본 실물형 + 사용자 양식만(끔 = S6 'game asset' 꼬리)
    named_nodetail: bool = True  # S22 APP_NAMED_NODETAIL: 이름만으로 정해지는 대상(헬로키티 등)은 상세화 없이 이름으로 T2I
    inpaint_crop: bool = False  # S7 APP_INPAINT_CROP: 마스크 주변만 잘라 인페인팅(기본 0, 사용자 결정 — 켬이 장면 재생성·R→B)

    @classmethod
    def from_env(cls) -> "Settings":
        data = Path(os.environ.get("APP_DATA_DIR") or "~/vhl-app-data").expanduser()
        return cls(
            data_dir=data,
            mock=_flag("APP_MOCK"),
            a5000_url=(os.environ.get("A5000_URL") or "").rstrip("/"),
            a5000_key=os.environ.get("A5000_KEY") or "",
            inpaint_port=int(os.environ.get("INPAINT_PORT") or 0),   # 값은 app.env 에만
            shared_token=os.environ.get("APP_SHARED_TOKEN") or "",
            bind_host=os.environ.get("APP_BIND_HOST") or "0.0.0.0",
            bind_port=int(os.environ.get("APP_BIND_PORT") or 0),
            render_mode=(os.environ.get("APP_RENDER_MODE") or "camera").strip(),
            mock_time_scale=float(os.environ.get("APP_MOCK_TIME_SCALE") or 1.0),
            mock_source=os.environ.get("APP_MOCK_SOURCE") or "sample-jeep",
            inpaint_crop=_flag("APP_INPAINT_CROP", "0"),
            realstyle=_flag("APP_REALSTYLE", "1"),
            relphrase=_flag("APP_RELPHRASE", "1"),
            worker_delete=_flag("APP_WORKER_DELETE", "0"),
            regionrefine=_flag("APP_REGIONREFINE", "1"),
            facehint=_flag("APP_FACEHINT", "1"),
            rejectraw=_flag("APP_REJECTRAW", "1"),
            inpaintprompt=_flag("APP_INPAINTPROMPT", "1"),
            named_nodetail=_flag("APP_NAMED_NODETAIL", "1"),
        )
