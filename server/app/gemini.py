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
DEFAULT_MODEL = "gemini-3.1-flash-lite-image"   # S20f-2: 비용 최소(Nano Banana 2 Lite)
TAIL = ("Edit only the {target}. Keep everything else identical: same character, pose, camera angle, framing, "
        "lighting, colors and plain white background. Output one image.")


class GeminiError(Exception):
    pass


_PNG_SIG = b"\x89PNG\r\n\x1a\n"
_CONVERT = ("import sys, io\nfrom PIL import Image\nim = Image.open(io.BytesIO(sys.stdin.buffer.read())).convert('RGB')\n"
            "o = io.BytesIO(); im.save(o, 'PNG'); sys.stdout.buffer.write(o.getvalue())")


def to_png(data: bytes) -> bytes:
    """Gemini 결과가 PNG 가 아니면(JPEG 등) PNG 로. 앱 venv 에 Pillow 가 없어 시스템 파이썬(Pillow)을 하위 프로세스로 부른다
    (빠른 시험용 우회, env GEMINI_CONVERT_PYTHON, 기본 /usr/bin/python3)."""
    if data[:8] == _PNG_SIG:
        return data
    import subprocess
    py = os.environ.get("GEMINI_CONVERT_PYTHON") or "/usr/bin/python3"
    try:
        r = subprocess.run([py, "-c", _CONVERT], input=data, capture_output=True, timeout=60, check=True)
    except (OSError, subprocess.SubprocessError) as e:
        raise GeminiError(f"Gemini 결과 이미지 변환 실패({py}): {type(e).__name__}: {e}") from e
    return r.stdout


def full_prompt(sentence: str, target: str, op: Optional[str] = None, old: Optional[str] = None,
                new: Optional[str] = None) -> str:
    """보정 문장 + (S20f-2: replace·remove 면 원래 물건 완전 제거 문장) + 고정 꼬리."""
    mid = ""
    if op == "replace" and old:
        mid = f"Completely remove the {old}; no part of it may remain. Put the {new or 'new object'} in its place. "
    elif op == "remove" and old:
        mid = f"Completely remove the {old} and fill with what would be behind it. "
    return f"{sentence.strip().rstrip('.')}. " + mid + TAIL.format(target=target or "selected part")


def _shrink_1024(png: bytes) -> bytes:
    a = render_rgb(decode_png(png))
    h, w = a.shape[:2]
    if max(h, w) <= 1024:
        return png
    k = 1024 / max(h, w)
    return encode_png(resize_lanczos(a, (round(h * k), round(w * k))))


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
    def __init__(self, model: Optional[str] = None, key: Optional[str] = None, timeout: float = 180.0, http=None,
                 cache_dir=None) -> None:
        self.model = model or os.environ.get("GEMINI_IMAGE_MODEL") or DEFAULT_MODEL
        self.key = key if key is not None else (os.environ.get("GEMINI_API_KEY") or "")
        self.timeout = timeout
        self.http = http                                    # 시험용 httpx.Client 대체
        self.cache_dir = cache_dir                          # S20f-2: 같은 호출 반복 금지(data_dir/gemini_cache)

    # ── S20f-2 캐시·호출 기록 ────────────────────────────────────
    def _key(self, image_png: bytes, prompt: str, model: str, loose: Optional[str]) -> str:
        import hashlib
        if loose:                                           # 시험 전용(GEMINI_CACHE_LOOSE): 자산·판본·시점·지시문·모델
            return hashlib.sha256(f"loose|{loose}|{model}".encode()).hexdigest()[:32]
        h = hashlib.sha256(image_png).hexdigest()
        return hashlib.sha256(f"{h}|{prompt}|{model}".encode()).hexdigest()[:32]

    def _log(self, rec: dict) -> int:
        import json as _j
        if self.cache_dir is None:
            return 0
        from pathlib import Path
        d = Path(self.cache_dir)
        d.mkdir(parents=True, exist_ok=True)
        p = d / "calls.jsonl"
        n = sum(1 for ln in p.read_text().splitlines() if '"cached": false' in ln) if p.is_file() else 0
        if not rec.get("cached"):
            n += 1
        rec["calls_total"] = n
        with open(p, "a", encoding="utf-8") as f:
            f.write(_j.dumps(rec, ensure_ascii=False) + "\n")
        return n

    def edit_cached(self, image_png: bytes, prompt: str, model: Optional[str] = None, loose: Optional[str] = None) -> tuple:
        """캐시 → 없으면 edit. info 에 cached·calls_total(실제 API 호출 누적)·usage."""
        import json as _j
        import time as _t
        from pathlib import Path
        model = model or self.model
        key = self._key(image_png, prompt, model, loose)
        if self.cache_dir is not None:
            d = Path(self.cache_dir)
            png, meta = d / f"{key}.png", d / f"{key}.json"
            if png.is_file() and meta.is_file():
                info = dict(_j.loads(meta.read_text()), cached=True, cache_key=key, latency_s=0.0)
                info["calls_total"] = self._log({"t": _t.time(), "model": model, "cached": True, "key": key})
                return png.read_bytes(), info
        out, info = self.edit(image_png, prompt, model=model)
        info.update(cached=False, cache_key=key)
        info["calls_total"] = self._log({"t": _t.time(), "model": model, "cached": False, "key": key,
                                         "latency_s": info.get("latency_s"), "usage": info.get("usage"),
                                         "image_config": info.get("image_config")})
        if self.cache_dir is not None:
            d = Path(self.cache_dir)
            d.mkdir(parents=True, exist_ok=True)
            (d / f"{key}.png").write_bytes(out)
            (d / f"{key}.json").write_text(_j.dumps({k: v for k, v in info.items() if k != "calls_total"}, ensure_ascii=False))
        return out, info

    def edit(self, image_png: bytes, prompt: str, model: Optional[str] = None) -> tuple:
        """→ (결과 PNG bytes, info{model, latency_s, prompt, finish, text})."""
        import httpx
        if not self.key:
            raise GeminiError("GEMINI_API_KEY 가 없다(app.env)")
        model = model or self.model
        image_png = _shrink_1024(image_png)                # S20f-2: 입력 1장, 1024² 이하
        # S20f-2: 출력 1장·1K·1:1, 이미지만. 모델이 옵션을 거부하면(400) 옵션을 하나씩 빼고 다시(기록)
        configs = [{"responseModalities": ["IMAGE"], "candidateCount": 1,
                    "imageConfig": {"imageSize": "1K", "aspectRatio": "1:1"}},
                   {"responseModalities": ["IMAGE"], "candidateCount": 1},
                   {"responseModalities": ["TEXT", "IMAGE"]}]
        t0 = time.time()
        client = self.http or httpx.Client(timeout=self.timeout)
        tried = []
        try:
            for cfg in configs:
                body = {"contents": [{"role": "user", "parts": [
                            {"text": prompt},
                            {"inline_data": {"mime_type": "image/png", "data": base64.b64encode(image_png).decode()}}]}],
                        "generationConfig": cfg}
                try:
                    r = client.post(API.format(model=model), json=body, headers={"x-goog-api-key": self.key})
                except httpx.HTTPError as e:
                    raise GeminiError(f"Gemini 연결 실패: {type(e).__name__}: {e}") from e
                tried.append({"config": list(cfg), "status": r.status_code})
                if r.status_code != 400:
                    break
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
                "usage": j.get("usageMetadata"), "image_config": cfg, "tried": tried}
        if img is None:
            raise GeminiError(f"Gemini 응답에 이미지가 없다(finish {info['finish']}, text {text[:120]!r})")
        d = img.get("inlineData") or img.get("inline_data")
        info["mime"] = d.get("mimeType") or d.get("mime_type")
        return to_png(base64.b64decode(d["data"])), info
