# W28 — multipart 전송 확인 · 판본 동기화 · 커밋 전 필수파일 검사

## ★ multipart 전송 — **내 쪽은 정상이다**

요청을 가로채 본문 구조를 직접 봤다:

```
content-type : multipart/form-data; boundary=…
본문 크기     : 805,617 B  (이미지 805,258 B)
meta 파트     : True
image 파트    : True · filename="condition.png" · PNG 매직 True
```

⇒ 이미지는 **확실히 나간다.** A5000 자기 보고("staging/{key}/cond.png 로 보관만
합니다")와 합치면 **전송 문제가 아니라 소비 문제**다. A5000 이 고치는 중이다.

⚠️ A5000 파일시스템 대조는 **못 했다** — `ssh` 가 `kex_exchange_identification` 으로
   반복 거절했다. 내 쪽 증거(위 본문 구조)와 A5000 자기 보고로 갈랐다.

🔴 그래서 내 W27 후보 셋(64³ 해상도 · 머리 비율 · 마스크 크기)은 **아직 검증 대상이
   아니다.** 조건이 실제로 들어간 뒤에 본다. 지금 조건 이미지 구도를 바꾸면 엉뚱한
   걸 고친다 — 규격("전신")도 그대로 뒀다.

## ★ 판본 동기화 — **409 를 받은 뒤에** 판단한다

🔴 처음엔 `GET committed.json` 으로 **사전 조회**해 판단하게 짰다. **틀렸다** —
그 엔드포인트가 없어서 404 였고, 나는 그것을 "커밋 안 됨" 으로 읽었다. 실제로는
커밋돼 있었고 그 자산은 편집이 성공한 적도 있다. **없는 것을 근거로 추측하면
멀쩡한 경로를 막는다.** 실제로 한 번 막았다(v1 이 거짓으로 차단됨).

지금은 상류가 **실제로 409 VERSION_CONFLICT 를 준 뒤에만** 그 경로를 탄다:

| base_version | 결과 |
|---|---|
| **v1** (상류에 커밋됨) | **succeeded** · changed 26 / removed 1 — 회귀 없음 |
| **v2** (상류에 없음) | `UPSTREAM_EDIT_FAILED` + **무엇을 밀어야 하는지** |

```
<EDIT_HOST> 편집 제출 실패 (409 VERSION_CONFLICT): base_version=2 은 커밋되지 않았다
(committed latest=1) · 3090 재료는 갖췄다 (청크 376 · slat.safetensors · source.png)
— 상류가 이 판본을 받아 커밋해야 한다
```

⚠️ **`recolor` 는 3090 로컬이라 그 결과 판본이 상류에 아예 없다.** 그 위에 형태 편집을
   걸면 여기로 온다 — 그때 이 메시지가 "무엇이 없고 무엇을 갖췄는지" 를 말한다.

⚠️ **밀어 넣는 경로 자체는 아직 없다.** 상류에 3090 판본을 커밋시키는 API 가
   `commit`(job_id + staging 필요)뿐인데, 로컬 편집본에는 staging 이 없다.
   그 절차는 A5000 과 합의가 필요하다.

## ★ 커밋 전 필수파일 검사 (`server/versionsync.py` · 테스트 9건)

A5000 `V1-REQUIRED-FILES.md` 실측대로, **202 뒤 워커에서 죽는** 셋을 앞당겨 막는다:

| 검사 | 없으면 |
|---|---|
| `slat.safetensors` | `_load_slat` → job INTERNAL |
| `input.png` (또는 `source.png`) | `pipe.get_cond` → job INTERNAL |
| slat 메타 `norm_mean`·`norm_std`·`slat_space` | `op_edit` KeyError — **다음 편집이 불가능한 막다른 판본** |
| `slat_space == "denormalized"` | 정규화 상태면 **조용히 틀린 기하** (예외가 안 난다) |
| 청크 0개 | 빈 판본을 만들지 않는다 |

내 `moto-b/slat.safetensors` 는 `norm_mean`·`norm_std`·`slat_space=denormalized` 를
모두 갖고 있다 (검사 통과).

## ★ 연결 실패도 상류 사유다

`ConnectError` 가 `INTERNAL` 로 나가면 화면이 "서버 버그" 라고 말하는 셈인데,
실제로는 상대가 내려가 있는 것이고 **고칠 사람이 다르다** (D71).
이제 `UPSTREAM_UNREACHABLE` · `UPSTREAM_TIMEOUT` 로 갈린다.

⚠️ httpx 예외 메시지에는 URL 이 들어갈 수 있다 — **예외 종류만 쓰고 메시지는 안 싣는다.**
   테스트가 IP·URL 부재를 잠근다 (§7).
