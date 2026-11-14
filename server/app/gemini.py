"""S20f Gemini 이미지 편집(마스크 없음, 프롬프트만) — REST generateContent(httpx, 새 의존성 없음).

토글 `APP_INPAINT_BACKEND=gemini`(기본 local). 모델 `GEMINI_IMAGE_MODEL`(기본 gemini-3-pro-image), 키 `GEMINI_API_KEY`(app.env, 값은 기록하지 않음).
"""

from __future__ import annotations

import base64
import os
import time
from typing import Optional

import numpy as np

from .cropinpaint import decode_png, encode_png, render_rgb, resize_lanczos

API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
DEFAULT_MODEL = "gemini-3-pro-image"
TAIL = ("Edit only the {target}. Keep everything else identical: same character, pose, camera angle, framing, "
        "lighting, colors and plain white background. Output one image.")


class GeminiError(Exception):
    pass


def full_prompt(sentence: str, target: str) -> str:
    return f"{sentence.strip().rstrip('.')}. " + TAIL.format(target=target or "selected part")


def fit_to(png: bytes, h: int, w: int) -> bytes:
    """결과를 렌더 크기로: 비율을 지키며 줄이고/키우고, 남는 곳은 흰색으로 가운데 맞춤. RGB PNG."""
    a = render_rgb(decode_png(png))
    ih, iw = a.shape[:2]
    if (ih, iw) == (h, w):
        return encode_png(a)
    k = min(h / ih, w / iw)
    nh, nw = max(1, round(ih * k)), max(1, round(iw * k))
    r = resize_lanczos(a, (nh, nw))
    out = np.full((h, w, 3), 255, np.uint8)
    y0, x0 = (h - nh) // 2, (w - nw) // 2
    out[y0:y0 + nh, x0:x0 + nw] = r
    return encode_png(out)


def outside_diff(render_png: bytes, edit_png: bytes, mask_png: bytes) -> dict:
    """마스크(라쏘 투영) 밖 화소의 원본 대비 평균 절대 차이(0–255, RGB 평균). 로그용."""
    from .cropinpaint import mask_bool
    r = render_rgb(decode_png(render_png)).astype(np.float32)
    e = render_rgb(decode_png(edit_png)).astype(np.float32)
    m = mask_bool(decode_png(mask_png))
    if r.shape != e.shape:
        return {"error": f"크기 다름 {r.shape} vs {e.shape}"}
    d = np.abs(r - e).mean(axis=2)
    out = ~m
    obj = (r < 250).any(axis=2) & out                      # 마스크 밖 물체 화소(흰 배경 제외)
    return {"mean_outside": round(float(d[out].mean()), 2) if out.any() else None,
            "mean_outside_object": round(float(d[obj].mean()), 2) if obj.any() else None,
            "frac_outside_changed_gt20": round(float((d[out] > 20).mean()), 4) if out.any() else None}


class GeminiClient:
    def __init__(self, model: Optional[str] = None, key: Optional[str] = None, timeout: float = 180.0, http=None) -> None:
        self.model = model or os.environ.get("GEMINI_IMAGE_MODEL") or DEFAULT_MODEL
        self.key = key if key is not None else (os.environ.get("GEMINI_API_KEY") or "")
        self.timeout = timeout
        self.http = http                                    # 시험용 httpx.Client 대체

    def edit(self, image_png: bytes, prompt: str, model: Optional[str] = None) -> tuple:
        """→ (결과 PNG bytes, info{model, latency_s, prompt, finish, text})."""
        import httpx
        if not self.key:
            raise GeminiError("GEMINI_API_KEY 가 없다(app.env)")
        model = model or self.model
        body = {"contents": [{"role": "user", "parts": [
                    {"text": prompt},
                    {"inline_data": {"mime_type": "image/png", "data": base64.b64encode(image_png).decode()}}]}],
                "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]}}
        t0 = time.time()
        client = self.http or httpx.Client(timeout=self.timeout)
        try:
            r = client.post(API.format(model=model), json=body, headers={"x-goog-api-key": self.key})
        except httpx.HTTPError as e:
            raise GeminiError(f"Gemini 연결 실패: {type(e).__name__}: {e}") from e
        finally:
            if self.http is None:
                client.close()
        dt = round(time.time() - t0, 3)
        if r.status_code != 200:
            raise GeminiError(f"Gemini {r.status_code}: {r.text[:300]}")
        j = r.json()
        cands = j.get("candidates") or []
        parts = ((cands[0].get("content") or {}).get("parts") or []) if cands else []
        img = next((p for p in parts if (p.get("inlineData") or p.get("inline_data"))), None)
        text = " ".join(p.get("text", "") for p in parts if p.get("text"))[:500]
        info = {"model": model, "latency_s": dt, "prompt": prompt, "text": text,
                "finish": cands[0].get("finishReason") if cands else None,
                "usage": j.get("usageMetadata")}
        if img is None:
            raise GeminiError(f"Gemini 응답에 이미지가 없다(finish {info['finish']}, text {text[:120]!r})")
        d = img.get("inlineData") or img.get("inline_data")
        return base64.b64decode(d["data"]), info
