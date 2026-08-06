"""잡 레지스트리 — 생성·편집이 **같은 폴링 규약**을 쓴다 (`JobStatus`).

두 경로 다 수십 초라 동기 응답이 안 된다. 상태는 계약이 정한 다섯뿐이다:
`queued` · `running` · `succeeded` · `failed` · `cancelled`.

⚠️ 인메모리다. 프로세스가 죽으면 잡이 사라진다 — 영속화는 다음 웨이브다.
   그 사실을 `/healthz` 가 아니라 **여기 적어 둔다**: 재시작 뒤 폴링하면 404 가 나고,
   그게 "잡이 실패했다" 로 오독될 수 있다.

⚠️ 실패를 **조용히 성공으로 만들지 않는다.** 예외는 `failed` + `error_code` 로 남고
   `JobStatus.error` 에 사유가 그대로 실린다.
"""

from __future__ import annotations

import os
import threading
import traceback
import uuid
from typing import Callable, Dict, Optional

from deltacontract.schemas import JobStatus  # type: ignore[import-not-found]

__all__ = ["JOBS", "JobRegistry", "POLL_INTERVAL_S", "POLL_TIMEOUT_S"]

#: Unity 폴링 규약. 3090 실측 생성 1회 38초의 3배 + 큐 여유.
POLL_INTERVAL_S = 2
POLL_TIMEOUT_S = 120


class JobRegistry:
    def __init__(self) -> None:
        self._jobs: Dict[str, JobStatus] = {}
        self._lock = threading.Lock()

    def new(self, *, asset_id: Optional[str] = None,
            idempotency_key: Optional[str] = None) -> JobStatus:
        # 같은 idempotency_key 면 **새 잡을 만들지 않는다** (계약 3.15.5).
        if idempotency_key:
            with self._lock:
                for j in self._jobs.values():
                    if j.idempotency_key == idempotency_key:
                        return j
        job = JobStatus(job_id=f"j-{uuid.uuid4().hex[:12]}", state="queued",
                        asset_id=asset_id, idempotency_key=idempotency_key)
        with self._lock:
            self._jobs[job.job_id] = job
        return job

    def get(self, job_id: str) -> Optional[JobStatus]:
        with self._lock:
            return self._jobs.get(job_id)

    def _set(self, job_id: str, **fields) -> None:
        with self._lock:
            j = self._jobs.get(job_id)
            if j is not None:
                self._jobs[job_id] = j.model_copy(update=fields)

    def run(self, job_id: str, fn: Callable[[Callable[..., None]], dict]) -> None:
        """백그라운드 실행. `fn(progress)` 가 `JobStatus` 갱신 필드를 반환한다.

        ⚠️ `JOBS_EXECUTE=0` 이면 **띄우지 않고 queued 로 둔다.** 테스트 이음매다 —
           라우트 테스트가 잡을 실제로 돌리면 LLM·t2i·상류 GPU 를 태우고, 인터프리터
           종료 시 데몬 스레드가 남아 무관한 오류를 낸다(실측). 라우트가 무엇을
           **접수했는가**를 재는 테스트에 실행은 필요 없다.

           🔴 기본값은 실행이다. 끄는 쪽이 명시적이어야 운영에서 조용히 안 도는 일이 없다.
        """
        if os.environ.get("JOBS_EXECUTE", "1") == "0":
            self._set(job_id, stage="not-executed",
                      stage_detail="JOBS_EXECUTE=0 — 실행하지 않았다 (테스트 이음매)")
            return

        def progress(p: float, stage: str = "", detail: str = "") -> None:
            self._set(job_id, progress=float(p), stage=stage or None,
                      stage_detail=detail or None)

        def body() -> None:
            self._set(job_id, state="running", progress=0.0)
            try:
                out = fn(progress)
                self._set(job_id, state="succeeded", progress=1.0, **out)
            except Exception as exc:                       # noqa: BLE001
                self._set(job_id, state="failed",
                          error=f"{type(exc).__name__}: {exc}",
                          error_code=getattr(exc, "error_code", "INTERNAL"))
                traceback.print_exc()

        threading.Thread(target=body, name=f"job-{job_id}", daemon=True).start()


JOBS = JobRegistry()
