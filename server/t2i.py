"""prompt → RGBA PNG. **프로세스 경계를 넘는 것이 이 모듈의 전부다.**

────────────────────────────────────────────────────────────────────────
왜 subprocess 인가 — 선택이 아니라 제약이다
────────────────────────────────────────────────────────────────────────
Z-Image 와 BiRefNet 은 **numpy/torch 판본이 서로 충돌**한다. 한 프로세스에서 둘 다
임포트하면 죽는다 (W2 실측). 그래서 각자의 conda 환경 파이썬으로 **따로 띄우고
파일로만 주고받는다** — `ai-ar-prototype` 의 t2i 설계를 그대로 승계한 것이고,
리포 밖 `_tools/` 의 두 스크립트가 이미 그 규약으로 돌고 있다.

⇒ 여기서 새로 만들지 않는다. **이미 도는 스크립트를 부른다.**
   재구현하면 두 벌이 되고, 그때부터 "어느 쪽이 진짜인가" 를 매번 확인해야 한다.

⚠️ 두 워커를 **동시에 띄우지 않는다.** Z-Image 가 카드 대부분을 쓴다. 순차 실행이다.
⚠️ 중간 RGB 를 지우지 않는다. 알파가 이상할 때 t2i 탓인지 분리 탓인지 가르는 유일한 근거다.
⚠️ 경로·환경은 전부 환경변수다 (§6). 이 파일에 홈 경로가 없다.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Optional, Tuple

__all__ = ["T2IUnavailable", "render_rgba", "tools_ready"]


class T2IUnavailable(RuntimeError):
    """t2i 체인을 못 돌린다. **대체 이미지를 만들어 내지 않는다.**"""

    error_code = "T2I_UNAVAILABLE"


def _cfg() -> Tuple[Path, Path, Path]:
    # ⚠️ 빈 문자열을 Path 로 만들면 `.` 이 되고 `.exists()` 가 **참**이다.
    #    그래서 변환 **전에** 원문을 본다 — 안 그러면 미설정이 통과하고,
    #    그 다음 subprocess 가 `./zimage_gen.py` 를 찾다가 엉뚱한 오류를 낸다.
    raw = {n: os.environ.get(n, "").strip()
           for n in ("T2I_TOOLS_DIR", "ZIMAGE_PYTHON", "BIREFNET_PYTHON")}
    missing = [n for n, v in raw.items() if not v]
    paths = {n: Path(v).expanduser() for n, v in raw.items() if v}
    missing += [n for n, p in paths.items() if not p.exists()]
    if missing:
        raise T2IUnavailable(
            f"t2i 설정이 없다: {', '.join(missing)}. 환경변수로만 받는다 (§6) — "
            "경로를 코드에 박지 않는다")
    return (paths["T2I_TOOLS_DIR"], paths["ZIMAGE_PYTHON"],
            paths["BIREFNET_PYTHON"])


def tools_ready() -> bool:
    try:
        _cfg()
        return True
    except T2IUnavailable:
        return False


#: 구도 강제. 슬롯마다 꼬리말을 다시 쓰면 한 군데서 빠뜨려도 증상이 없고
#: 그 자산만 조용히 다른 구도로 나온다 (W17 에서 템플릿으로 뺀 이유).
#: S6(2026-11-11): 논문 X3 형식의 고정 꼬리로 바꿨다. 앞부분 {subject} 는 앱 서버가 LLM 으로 상세화한 물체 묘사다.
#: `no shadow` 는 서버 고정 문구라 LLM 출력 부정어 검사 대상이 아니다.
DEFAULT_TEMPLATE = os.environ.get(
    "T2I_PROMPT_TEMPLATE",
    "{subject}, three-quarter front view, centered, full object visible, "
    "simple clean 3D game asset style, pure white background, no shadow")


#: S11(2026-11-12): 앱 생성의 기본은 실물형 — `simple clean 3D game asset style` 삭제. 사용자가 양식을 말하면
#: `{style} style` 을 넣는다. 앱 서버(`APP_REALSTYLE=1`)가 template= 로 넘긴다. 다른 경로(routes_v2·voxhammer)는 DEFAULT_TEMPLATE 그대로.
REAL_TEMPLATE = "{subject}, three-quarter front view, centered, full object visible, pure white background, no shadow"
REAL_STYLE_TEMPLATE = ("{subject}, {style} style, three-quarter front view, centered, full object visible, "
                       "pure white background, no shadow")


def app_template(style: Optional[str]) -> str:
    """S11: 양식(None = 기본 실물형) → T2I 템플릿({subject} 만 남김)."""
    return REAL_STYLE_TEMPLATE.replace("{style}", style) if style else REAL_TEMPLATE


_SETTING_LINE = ("from_pretrained", "height", "width", "num_inference_steps", "guidance", "Resize", "torch_dtype",
                 "manual_seed", "max_sequence_length")


def _script_settings(path: Path) -> list:
    """S9: 스크립트 소스에서 설정이 들어 있는 줄을 그대로 뽑는다(모델·해상도·스텝·guidance 등)."""
    try:
        return [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines()
                if any(k in ln for k in _SETTING_LINE)]
    except OSError as e:
        return [f"(읽지 못함: {e})"]


def render_rgba(prompt: str, *, seed: int = 42,
                keep_dir: Optional[Path] = None, template: Optional[str] = None, return_rgb: bool = False):
    """prompt → (RGBA PNG 바이트, 소요 내역).

    Raises:
        T2IUnavailable: 설정이 없거나 워커가 실패했다. **빈 이미지를 내지 않는다** —
            자리표시를 내면 그 뒤 파이프라인이 전부 정상 동작하면서 다른 물체를 만든다.
    """
    tools, zpy, bpy = _cfg()
    work = Path(keep_dir) if keep_dir else Path(tempfile.mkdtemp(prefix="t2i-"))
    work.mkdir(parents=True, exist_ok=True)
    rgb, rgba = work / "image.png", work / "source.png"
    effective = (template or DEFAULT_TEMPLATE).replace("{subject}", prompt)
    (work / "prompt.effective.txt").write_text(effective, encoding="utf-8")

    timing: dict = {"prompt_effective_chars": len(effective), "prompt_effective": effective,
                    # S9: 실제로 부른 명령(경로는 이름만)과 스크립트 안 설정(파일에서 읽음)
                    "command": [["zimage_gen.py", "--prompt", effective, "--out", "image.png", "--seed", str(seed)],
                                ["birefnet_seg.py", "--in", "image.png", "--out", "source.png"]],
                    "settings": {"source": "T2I_TOOLS_DIR 스크립트 소스에서 읽은 줄(인자로 넘기지 않는 값은 스크립트 고정)",
                                 "zimage_gen.py": _script_settings(tools / "zimage_gen.py"),
                                 "birefnet_seg.py": _script_settings(tools / "birefnet_seg.py")}}
    try:
        t0 = time.time()
        subprocess.run([str(zpy), str(tools / "zimage_gen.py"),
                        "--prompt", effective, "--out", str(rgb), "--seed", str(seed)],
                       check=True, capture_output=True, timeout=900)
        timing["t2i_s"] = round(time.time() - t0, 1)

        # ⚠️ 순차다. 위 프로세스가 끝난 뒤에 띄운다 (VRAM 회수 여백).
        t1 = time.time()
        subprocess.run([str(bpy), str(tools / "birefnet_seg.py"),
                        "--in", str(rgb), "--out", str(rgba)],
                       check=True, capture_output=True, timeout=900)
        timing["segment_s"] = round(time.time() - t1, 1)
    except subprocess.CalledProcessError as e:
        tail = (e.stderr or b"")[-400:].decode("utf-8", "replace")
        raise T2IUnavailable(f"t2i 워커 실패 (rc={e.returncode}): {tail}") from e
    except subprocess.TimeoutExpired as e:
        raise T2IUnavailable(f"t2i 워커 제한 시간 초과: {e.cmd[0]}") from e

    if not rgba.is_file() or rgba.stat().st_size == 0:
        raise T2IUnavailable("분리 결과가 비었다 — 자리표시를 대신 내지 않는다")
    data = rgba.read_bytes()
    rgb_data = rgb.read_bytes() if (return_rgb and rgb.is_file()) else None   # S14: 배경 제거 전 원본
    timing["rgb_bytes"] = rgb.stat().st_size if rgb.is_file() else 0
    timing["rgba_bytes"] = len(data)
    if keep_dir is None:
        shutil.rmtree(work, ignore_errors=True)
    return (data, timing, rgb_data) if return_rgb else (data, timing)
