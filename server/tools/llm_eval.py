"""S2 확인 — 보정 20건(한국어 10 + 영어 10) · 분류 15건(prompt·quality·region 각 5)을 실제 모델로 돌린다.

    python -m server.tools.llm_eval --model claude-haiku-5-5 --out <결과.json>

키·모델은 `app.env`(이미 설정된 환경변수 우선). 결과 JSON 에 건별 출력·위반·재시도·지연·토큰을 남긴다.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from server.app.config import load_env_file
from server.app.llm_adapter import ClaudeAdapter, LLMInvalid, LLMUnavailable

MOTO = "a man riding a black motorcycle"
SNOW = "a snowman with a carrot nose, stick arms and a red scarf"
CHAIR = "a wooden armchair with armrests"
TRUCK = "a red pickup truck"
JEEP = "a green jeep with a black soft top"
COTTAGE = "a small wooden cottage with a stone chimney"

# (장면, 지시, 기대 op, 언어)
REFINE = [
    (MOTO, "make the rider wear a red and gold Iron Man helmet", "replace", "en"),
    (MOTO, "add a sidecar to the motorcycle", "add", "en"),
    (MOTO, "remove the rider", "remove", "en"),
    (MOTO, "paint the motorcycle bright yellow", "recolor", "en"),
    (SNOW, "replace the carrot nose with a small red ball", "replace", "en"),
    (SNOW, "add a black top hat", "add", "en"),
    (CHAIR, "remove the armrests", "remove", "en"),
    (CHAIR, "make the chair white", "recolor", "en"),
    (TRUCK, "put a black roof rack on top", "add", "en"),
    (COTTAGE, "change the chimney into a round stone tower", "replace", "en"),
    (MOTO, "타는 사람을 해골로 바꿔줘", "replace", "ko"),
    (MOTO, "오토바이 뒤에 짐가방을 달아줘", "add", "ko"),
    (MOTO, "남자를 없애줘", "remove", "ko"),
    (MOTO, "오토바이를 빨간색으로 칠해줘", "recolor", "ko"),
    (SNOW, "목도리를 파란색으로 바꿔", "recolor", "ko"),
    (SNOW, "막대기 팔을 없애줘", "remove", "ko"),
    (SNOW, "오른손에 빗자루를 들려줘", "add", "ko"),
    (CHAIR, "등받이를 세로 나무살로 바꿔줘", "replace", "ko"),
    (JEEP, "앞 범퍼에 불바를 달아줘", "add", "ko"),
    (COTTAGE, "굴뚝을 없애줘", "remove", "ko"),
]

R_HELMET = {"target_element": "the man's head", "replacement": "a red and gold Iron Man helmet", "op": "replace",
            "new_scene_prompt": "a man wearing a red and gold Iron Man helmet riding a black motorcycle"}
R_WHEEL = {"target_element": "the rear tire", "replacement": "a thick knobby off-road tire", "op": "replace",
           "new_scene_prompt": "a man riding a black motorcycle with a thick knobby off-road rear tire"}
R_NOSE = {"target_element": "the carrot nose", "replacement": "a pumpkin nose", "op": "replace",
          "new_scene_prompt": "a snowman with a pumpkin nose, stick arms and a red scarf"}

# (이유, 보정 맥락, 기대 분류)
CLASSIFY = [
    ("헬멧이 아니라 모자가 나왔어", R_HELMET, "prompt"),
    ("the helmet should be gold, not silver", R_HELMET, "prompt"),
    ("바퀴가 너무 얇아, 훨씬 두껍게 해줘", R_WHEEL, "prompt"),
    ("I asked for a pumpkin, it drew an orange ball", R_NOSE, "prompt"),
    ("만화 같은 스타일 말고 사실적인 헬멧으로", R_HELMET, "prompt"),
    ("the image is blurry", R_HELMET, "quality"),
    ("모양이 찌그러졌어", R_WHEEL, "quality"),
    ("there are weird artifacts around the edges", R_NOSE, "quality"),
    ("얼굴이 뭉개져서 알아볼 수 없어", R_HELMET, "quality"),
    ("the texture looks smeared and low detail", R_WHEEL, "quality"),
    ("wrong part, I wanted the front wheel", R_WHEEL, "region"),
    ("내가 고른 곳이 아니라 다른 데가 바뀌었어", R_NOSE, "region"),
    ("the edit should cover the whole head, not just the top half", R_HELMET, "region"),
    ("왼팔이 아니라 오른팔을 골랐는데", R_NOSE, "region"),
    ("선택 영역 밖까지 바뀌었어", R_WHEEL, "region"),
]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    load_env_file()
    calls: list = []
    ad = ClaudeAdapter(model=a.model, on_call=calls.append)
    res = {"model": a.model, "effort": ad.effort, "started": time.strftime("%Y-%m-%d %H:%M:%S"), "refine": [], "classify": []}
    for i, (scene, ins, exp, lang) in enumerate(REFINE, 1):
        n0 = len(calls)
        try:
            out, err = ad.refine(scene, ins, []), None
        except (LLMInvalid, LLMUnavailable) as e:
            out, err = None, f"{type(e).__name__}: {e}"
        c = calls[n0]
        res["refine"].append({"i": i, "lang": lang, "scene": scene, "instruction": ins, "expected_op": exp, "output": out,
                              "error": err, "op_match": bool(out and out["op"] == exp), "retried": c["retried"],
                              "latency_s": c["latency_s"], "in_tokens": c["in_tokens"], "out_tokens": c["out_tokens"]})
        print(f"R{i:02d} {lang} {exp:7s} → {out and out['op']} | {out and out['new_scene_prompt']} | "
              f"retry={c['retried']} {c['latency_s']}s {c['in_tokens']}/{c['out_tokens']} {err or ''}", flush=True)
    for i, (reason, ref, exp) in enumerate(CLASSIFY, 1):
        n0 = len(calls)
        try:
            out, err = ad.classify(reason, ref, []), None
        except (LLMInvalid, LLMUnavailable) as e:
            out, err = None, f"{type(e).__name__}: {e}"
        c = calls[n0]
        res["classify"].append({"i": i, "reason": reason, "expected": exp, "output": out, "error": err,
                                "correct": bool(out and out["category"] == exp), "retried": c["retried"],
                                "latency_s": c["latency_s"], "in_tokens": c["in_tokens"], "out_tokens": c["out_tokens"]})
        print(f"C{i:02d} {exp:7s} → {out and out['category']} | {out and out['guidance']} | retry={c['retried']} "
              f"{c['latency_s']}s {c['in_tokens']}/{c['out_tokens']} {err or ''}", flush=True)
    R, C = res["refine"], res["classify"]
    tok = lambda xs: (sum(x["in_tokens"] for x in xs), sum(x["out_tokens"] for x in xs))  # noqa: E731
    avg = lambda xs, k: round(sum(x[k] for x in xs) / len(xs), 2)                         # noqa: E731
    res["summary"] = {
        "refine_ok": sum(x["output"] is not None for x in R), "refine_retried": sum(x["retried"] for x in R),
        "refine_failed": sum(x["error"] is not None for x in R), "refine_op_match": sum(x["op_match"] for x in R),
        "classify_correct": sum(x["correct"] for x in C), "classify_retried": sum(x["retried"] for x in C),
        "classify_failed": sum(x["error"] is not None for x in C),
        "latency_refine_avg_s": avg(R, "latency_s"), "latency_classify_avg_s": avg(C, "latency_s"),
        "tokens_refine_in_out": tok(R), "tokens_classify_in_out": tok(C),
        "tokens_total_in_out": tuple(map(sum, zip(tok(R), tok(C)))),
        "tokens_per_edit_avg_in_out": (round(2 * tok(R)[0] / len(R) + tok(C)[0] / len(C)),
                                       round(2 * tok(R)[1] / len(R) + tok(C)[1] / len(C))),
    }
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(res, indent=1, ensure_ascii=False))
    print("SUMMARY", json.dumps(res["summary"], ensure_ascii=False))


if __name__ == "__main__":
    main()
