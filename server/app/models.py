"""app-api 1.0.1 응답·요청 모델. **실제 모드와 가짜 모드가 같은 모델을 쓴다** (계약 7절).

필드 이름·모양은 `docs/app-api-v1.md` 그대로다. 4.0 표의 "항상 채움" 필드는 Optional 이 아니다.
"""

from __future__ import annotations

import math
from typing import Any, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator


# ── 요청 ────────────────────────────────────────────────────────────
class CreateAssetReq(BaseModel):
    prompt: str = Field(min_length=1, max_length=300)
    seed: int = 42


class Camera(BaseModel):
    frame: str
    position: List[float]
    forward: List[float]
    up: List[float]
    fov_y_deg: float
    aspect: float
    screen_px: Optional[List[int]] = None


class EditReq(BaseModel):
    base_version: int
    instruction: str = Field(min_length=1, max_length=300)
    camera: Optional[Camera] = None          # S20 1.1.0: camera·volume_cells 둘 다 없으면 에이전트 편집
    volume_cells: Optional[List[List[int]]] = None
    agent: Optional[bool] = None             # S20e 1.1.1: true + camera + volume_cells = 에이전트가 라쏘 볼륨을 다듬는 편집


class Review2DReq(BaseModel):
    accept: bool
    reason: Optional[str] = None


class Review3DReq(BaseModel):
    keep: bool


class RatingReq(BaseModel):
    stars: int = Field(ge=0, le=5)
    review: Optional[str] = Field(default=None, max_length=1000)      # S21 1.2.0
    signer_pubkey: Optional[str] = None                                # S21: 0x04‖x‖y(기기 Keystore P-256)


class PrepareReq(BaseModel):
    signer_pubkey: Optional[str] = None


class SubmittedReq(BaseModel):
    user_op_hash: Optional[str] = None
    signature: Optional[str] = None


# ── 응답 ────────────────────────────────────────────────────────────
class ActiveJob(BaseModel):
    job_id: str
    kind: str
    state: str
    is_mine: bool


class QrResp(BaseModel):
    qr_id: str
    asset_id: str
    version: int
    is_creator: bool
    scene_prompt: Optional[str]
    manifest_uri: Optional[str]
    slat_coords_uri: Optional[str]
    active_job: Optional[ActiveJob]
    can_rate: bool
    rate_range: Optional[List[int]]


class CreateResp(BaseModel):
    job_id: str
    asset_id: str


class EditCreated(BaseModel):
    job_id: str


class Refine(BaseModel):
    target_element: Optional[str]
    replacement: Optional[str]
    op: Optional[str]
    new_scene_prompt: Optional[str]


class Preview(BaseModel):
    attempt: int
    render_url: str
    edit_url: str
    mask_url: str


class Review(BaseModel):
    reason: str
    category: Literal["prompt", "quality", "region"]
    guidance: Optional[str]


class Prompts(BaseModel):
    """app-api 1.0.5 4.4 — 항상 7키, 값은 없으면 null."""
    input: Optional[str]
    detailed: Optional[str]
    style: Optional[str]
    inpaint: Optional[str]
    mask_ratio: Optional[float]
    region_fit: Optional[str]
    refine_retried: Optional[bool]


class JobObj(BaseModel):
    job_id: str
    kind: Literal["generate", "edit"]
    asset_id: str
    qr_id: Optional[str]
    is_mine: bool
    state: str
    stage: str
    progress: float
    queue_pos: Optional[int]
    created_at: float
    updated_at: float
    elapsed_s: float
    attempt: Optional[int]
    max_attempts: Optional[int]
    refine: Optional[Refine]
    preview: Optional[Preview]
    review: Optional[Review]
    prompts: Prompts
    result: Optional[dict]
    expires_at: Optional[float]
    error_code: Optional[str]
    error_message: Optional[str]
    mode: Optional[str] = None               # S20 10.3: "agent" 일 때만
    agent: Optional[dict] = None
    events: Optional[List[dict]] = None


class RatingResp(BaseModel):
    rating_id: str
    asset_id: str
    stars: int
    value: int
    decimals: int
    tag1: str
    version_range: List[int]
    feedback_uri: str
    feedback_hash: str


class Workers(BaseModel):
    a5000: str
    inpaint: str
    llm: str


class HealthResp(BaseModel):
    ok: bool
    app_api: str
    contract_version: int
    mock: bool
    workers: Optional[Workers]
    features: List[str] = []                 # S20 10.4: "agent_edit"
    chain: Optional[dict] = None             # S21 11.5


class Sample(BaseModel):
    name: str
    asset_id: str
    versions: List[int]
    note: str


class SamplesResp(BaseModel):
    samples: List[Sample]


class ErrorBody(BaseModel):
    error_code: str
    message: str
    detail: Optional[dict[str, Any]] = None


# ── 카메라 검사 (계약 2.2) ──────────────────────────────────────────
def camera_problem(c: Camera) -> Optional[str]:
    if c.frame != "normalized_voxel":
        return f"frame 은 normalized_voxel 이어야 한다: {c.frame!r}"
    for n in ("position", "forward", "up"):
        v = getattr(c, n)
        if len(v) != 3 or not all(math.isfinite(x) for x in v):
            return f"{n} 는 유한한 3성분이어야 한다"
    if math.sqrt(sum(x * x for x in c.position)) <= 0.5:
        return "position 이 자산 정육면체 안이다(원점 거리 ≤ 0.5)"
    nf = math.sqrt(sum(x * x for x in c.forward))
    nu = math.sqrt(sum(x * x for x in c.up))
    if nf == 0 or nu == 0:
        return "forward·up 은 0 벡터가 아니어야 한다"
    cos = sum(a * b for a, b in zip(c.forward, c.up)) / (nf * nu)
    ang = math.degrees(math.acos(max(-1.0, min(1.0, cos))))
    if not 80.0 <= ang <= 100.0:
        return f"forward·up 사이 각이 80–100° 밖이다: {ang:.1f}°"
    if not 10.0 <= c.fov_y_deg <= 120.0:
        return f"fov_y_deg 는 10–120: {c.fov_y_deg}"
    if not 0.2 <= c.aspect <= 5.0:
        return f"aspect 는 0.2–5: {c.aspect}"
    return None
