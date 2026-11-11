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
from server.app.llm_adapter import _NEG, ClaudeAdapter, LLMInvalid, LLMUnavailable, is_english

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


GEN = [("검은 오토바이를 탄 남자", "ko"), ("빨간 목도리를 한 눈사람", "ko"), ("등받이가 높은 나무 의자", "ko"),
       ("a red pickup truck", "en"), ("a small wooden cottage with a stone chimney", "en"), ("a blue ceramic teapot", "en")]


def s4(ad, calls, out):
    """S4: 생성 프롬프트 변환 6건 + 보정 20건(부정어 수)."""
    res = {"model": ad.model, "translate": [], "refine": []}
    for p, lang in GEN:
        n0 = len(calls)
        if is_english(p):
            res["translate"].append({"prompt": p, "lang": lang, "prompt_en": p, "skipped": True})
            print(f"T {lang} {p} → (영어, 그대로)")
            continue
        try:
            en, err = ad.detail_prompt(p), None
        except (LLMInvalid, LLMUnavailable) as e:
            en, err = None, f"{type(e).__name__}: {e}"
        c = calls[n0]
        res["translate"].append({"prompt": p, "lang": lang, "prompt_en": en, "error": err, "retried": c["retried"],
                                 "latency_s": c["latency_s"], "in_tokens": c["in_tokens"], "out_tokens": c["out_tokens"]})
        print(f"T {lang} {p} → {en} | retry={c['retried']} {c['latency_s']}s {c['in_tokens']}/{c['out_tokens']} {err or ''}")
    for i, (scene, ins, exp, lang) in enumerate(REFINE, 1):
        n0 = len(calls)
        try:
            o, err = ad.refine(scene, ins, []), None
        except (LLMInvalid, LLMUnavailable) as e:
            o, err = None, f"{type(e).__name__}: {e}"
        c = calls[n0]
        neg = [m.group(0) for k in ("new_scene_prompt", "replacement") for m in [_NEG.search((o or {}).get(k, ""))] if m]
        first_neg = any("부정" in (a.get("problem") or "") for a in c.get("attempts", []))
        res["refine"].append({"i": i, "lang": lang, "instruction": ins, "expected_op": exp, "output": o, "error": err,
                              "negations_final": neg, "negation_on_first_try": first_neg, "retried": c["retried"],
                              "latency_s": c["latency_s"], "in_tokens": c["in_tokens"], "out_tokens": c["out_tokens"]})
        print(f"R{i:02d} {lang} {exp:7s} → {o and o['op']} | {o and o['new_scene_prompt']} | neg={neg} first_neg={first_neg} "
              f"retry={c['retried']} {c['latency_s']}s {c['in_tokens']}/{c['out_tokens']} {err or ''}")
    R = res["refine"]
    res["summary"] = {"refine_ok": sum(x["output"] is not None for x in R), "negations_final": sum(bool(x["negations_final"]) for x in R),
                      "negation_retries": sum(x["negation_on_first_try"] for x in R), "retried": sum(x["retried"] for x in R),
                      "op_match": sum(bool(x["output"] and x["output"]["op"] == x["expected_op"]) for x in R),
                      "latency_avg_s": round(sum(x["latency_s"] for x in R) / len(R), 2),
                      "tokens_in_out": (sum(x["in_tokens"] for x in R), sum(x["out_tokens"] for x in R))}
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(json.dumps(res, indent=1, ensure_ascii=False))
    print("SUMMARY", json.dumps(res["summary"], ensure_ascii=False))


S6_GEN = [("A compact off-road jeep", "en"), ("a golden tea pot", "en"), ("검은 오토바이를 탄 남자", "ko"),
          ("빨간 목도리를 한 눈사람", "ko")]
S6_EDIT = [("a golden tea pot", "handle into the red tennis ball"),
           ("A compact off-road jeep", "change the front bumper into a chrome bull bar")]


def s6(ad, calls, out):
    """S6: 거친 생성 입력 → subject_detailed → T2I 문장, 편집 지시 → new_scene_prompt → 인페인팅 문장."""
    from server import t2i
    from server.app.engine import INPAINT_SUFFIX
    res = {"model": ad.model, "template": t2i.DEFAULT_TEMPLATE, "inpaint_suffix": INPAINT_SUFFIX, "generate": [], "edit": []}
    details = {}
    for p, lang in S6_GEN:
        n0 = len(calls)
        try:
            d, err = ad.detail_prompt(p), None
        except (LLMInvalid, LLMUnavailable) as e:
            d, err = p, f"{type(e).__name__}: {e}"
        c = calls[n0]
        details[p] = d
        row = {"input": p, "lang": lang, "subject_detailed": d, "t2i_prompt": t2i.DEFAULT_TEMPLATE.replace("{subject}", d),
               "chars": len(d), "error": err, "retried": c["retried"], "latency_s": c["latency_s"],
               "in_tokens": c["in_tokens"], "out_tokens": c["out_tokens"],
               "problems": [a.get("problem") for a in c.get("attempts", []) if a.get("problem")]}
        res["generate"].append(row)
        print(f"G {lang} {p!r} → {d!r} ({len(d)}자) retry={c['retried']} {c['latency_s']}s {c['in_tokens']}/{c['out_tokens']} {err or ''}")
    for base, ins in S6_EDIT:
        scene = details.get(base) or base
        n0 = len(calls)
        try:
            o, err = ad.refine(scene, ins, []), None
        except (LLMInvalid, LLMUnavailable) as e:
            o, err = None, f"{type(e).__name__}: {e}"
        c = calls[n0]
        nsp = (o or {}).get("new_scene_prompt", "")
        row = {"scene": scene, "instruction": ins, "output": o, "inpaint_prompt": nsp.rstrip(" .") + INPAINT_SUFFIX if o else None,
               "error": err, "retried": c["retried"], "latency_s": c["latency_s"], "in_tokens": c["in_tokens"],
               "out_tokens": c["out_tokens"]}
        res["edit"].append(row)
        print(f"E {ins!r} → {nsp!r} retry={c['retried']} {c['latency_s']}s {c['in_tokens']}/{c['out_tokens']} {err or ''}")
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(json.dumps(res, indent=1, ensure_ascii=False))


OLD_JEEP_EXAMPLE = ("chunky all-terrain tires", "black front winch bumper", "side air-intake snorkel",
                    "spare tire mounted on the rear door", "olive green paint")
NEW_EXAMPLE = ("rounded top", "red side flag", "pull-down door", "brass handle", "wooden post", "dark blue")


def jeep_repeat(ad, calls, out, n):
    """S6 추가: 'A compact off-road jeep' n 회 — 예전 지프 예시·새 예시 문구를 그대로 베끼는지."""
    p, rows = "A compact off-road jeep", []
    for i in range(1, n + 1):
        n0 = len(calls)
        try:
            d, err = ad.detail_prompt(p), None
        except (LLMInvalid, LLMUnavailable) as e:
            d, err = p, f"{type(e).__name__}: {e}"
        c, low = calls[n0], d.lower()
        rows.append({"i": i, "subject_detailed": d, "chars": len(d), "error": err, "retried": c["retried"],
                     "old_jeep_example_phrases": [x for x in OLD_JEEP_EXAMPLE if x in low],
                     "new_example_phrases": [x for x in NEW_EXAMPLE if x in low],
                     "latency_s": c["latency_s"], "in_tokens": c["in_tokens"], "out_tokens": c["out_tokens"]})
        r = rows[-1]
        print(f"J{i} {d!r} ({len(d)}자) old={r['old_jeep_example_phrases']} new={r['new_example_phrases']} "
              f"retry={c['retried']} {c['latency_s']}s {c['in_tokens']}/{c['out_tokens']} {err or ''}")
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(json.dumps({"model": ad.model, "input": p, "runs": rows}, indent=1, ensure_ascii=False))


# S8: Unity C4 j-85929860efd1 시도 2(prompt 거절) 상황
S8_SCENE = ("A wooden storage crate, a large white painted letter R on the front panel, slatted wooden sides, "
            "a flat rectangular lid, sturdy corner posts, metal corner brackets, natural brown wood")
S8_INS = "make the letter R red"
S8_PREV = ("A wooden storage crate, a large letter R painted in red on the front panel, slatted wooden sides, a flat "
           "rectangular lid, sturdy corner posts, metal corner brackets, natural brown wood")
S8_REASON = "this version is totally not what I asked"
S8_GUIDE = ("The user says the preview is not what they asked for, with no specific detail. Re-read the original request "
            "and keep the edit closely tied to it: recolor only the existing white letter R to red, keep it large and on "
            "the front panel, and avoid adding unrequested elements like the extra crate description or changed wood styling.")


def s8(ad, calls, out, n, scene, force=False):
    from server.app.engine import _norm
    rows = []
    for i in range(1, n + 1):
        n0 = len(calls)
        first = ad.refine(scene, S8_INS, [S8_GUIDE])
        same1 = _norm(first["new_scene_prompt"]) == _norm(S8_PREV)
        second = same2 = None
        if same1 or force:
            second = ad.refine(scene, S8_INS, [S8_GUIDE], changed_from={"prev": S8_PREV, "reason": S8_REASON,
                                                                        "guidance": S8_GUIDE})
            same2 = _norm(second["new_scene_prompt"]) == _norm(S8_PREV)
        cs = calls[n0:]
        rows.append({"i": i, "first": first, "first_same": same1, "second": second, "second_same": same2,
                     "calls": [{k: c[k] for k in ("kind", "latency_s", "in_tokens", "out_tokens", "retried")} for c in cs]})
        print(f"S8-{i} same1={same1} same2={same2}\n  1: {first['new_scene_prompt']}\n  2: {second and second['new_scene_prompt']}")
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(json.dumps({"model": ad.model, "scene": scene, "instruction": S8_INS, "prev": S8_PREV,
                                     "reason": S8_REASON, "guidance": S8_GUIDE, "runs": rows}, indent=1, ensure_ascii=False))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--s4", action="store_true", help="S4: 보정 20건 부정어 확인(+ 생성 6건)")
    ap.add_argument("--s6", action="store_true", help="S6: 거친 입력 6건 상세화·보정")
    ap.add_argument("--jeep-repeat", type=int, default=0, help="S6 추가: 'A compact off-road jeep' 상세화 n 회")
    ap.add_argument("--s8", type=int, default=0, help="S8: j-85929860efd1 시도 2 상황 재현 n 회")
    ap.add_argument("--s8-scene", default=S8_SCENE)
    ap.add_argument("--s8-force", action="store_true", help="첫 문장이 달라도 재요청 경로를 부른다(경로 확인용)")
    a = ap.parse_args()
    load_env_file()
    calls: list = []
    ad = ClaudeAdapter(model=a.model, on_call=calls.append)
    if a.s8:
        s8(ad, calls, a.out, a.s8, a.s8_scene, a.s8_force)
        return
    if a.jeep_repeat:
        jeep_repeat(ad, calls, a.out, a.jeep_repeat)
        return
    if a.s6:
        s6(ad, calls, a.out)
        return
    if a.s4:
        s4(ad, calls, a.out)
        return
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
