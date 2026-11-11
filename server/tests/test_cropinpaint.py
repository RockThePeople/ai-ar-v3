"""S7 잘라 인페인팅: 상자 계산, 마스크 밖 원본 유지, 끔 토글, 엔진·/clients 연결."""
import numpy as np

from server.app import cropinpaint as C


def _scene(mask_box=(400, 500, 460, 560), size=1024):
    rng = np.random.default_rng(0)
    r = rng.integers(0, 256, (size, size, 4), dtype=np.uint8)
    r[..., 3] = 255
    r[:100, :100, 3] = 0                                      # 투명 → 흰 배경
    r[100:110, :100, 3] = 128                                 # 반투명
    m = np.zeros((size, size, 1), np.uint8)
    x0, y0, x1, y1 = mask_box
    m[y0:y1, x0:x1] = 255
    return r, m


def test_crop_box_rules():
    m = np.zeros((1024, 1024), bool)
    m[500:560, 400:460] = True                                # 60×60 → 384(최소)
    assert C.crop_box(m) == (238, 338, 384)
    m[:] = False
    m[0:200, 0:100] = True                                    # 긴 변 200 → 400, 왼쪽 위로 밀림
    assert C.crop_box(m) == (0, 0, 400)
    m[:] = False
    m[100:700, 100:300] = True                                # 긴 변 600 → 1200 ≥ 1024 → 전체
    assert C.crop_box(m) == (0, 0, 1024)
    assert C.crop_box(np.zeros((8, 8), bool)) is None


def test_outside_mask_identical_and_inside_changed():
    r, m = _scene()
    sent = {}

    def call(rp, mp):
        sent["r"], sent["m"] = C.decode_png(rp), C.decode_png(mp)
        return C.encode_png(np.full((1024, 1024, 3), 7, np.uint8))
    out_png, rec, files = C.crop_inpaint(C.encode_png(r), C.encode_png(m), call)
    out = C.decode_png(out_png)
    base = C.render_rgb(r)
    mk = m[..., 0] > 127
    assert out.shape == (1024, 1024, 3)
    assert (out[~mk] == base[~mk]).all()                       # 마스크 밖 = 흰 배경 RGB 그대로
    assert (out[mk] != base[mk]).any() and (out[520:540, 420:440] == 7).all()   # 안쪽(경계 4 px 밖)은 결과
    assert rec["box"] == [238, 338, 384] and sent["r"].shape == (1024, 1024, 3)
    assert set(np.unique(sent["m"])) <= {0, 255} and sent["m"].mean() > m.mean()  # 마스크가 커져서 감
    assert set(files) == {"crop_render.png", "crop_mask.png", "crop_edit.png"}


def test_feather_inside_only():
    m = np.zeros((20, 20), bool)
    m[5:15, 5:15] = True
    a = C.feather_alpha(m, 4)
    assert (a[~m] == 0).all() and a[5, 10] == 0.25 and a[9, 10] == 1.0


def test_empty_mask_passthrough():
    r, m = _scene()
    m[:] = 0
    out, rec, files = C.crop_inpaint(C.encode_png(r), C.encode_png(m), lambda rp, mp: b"X")
    assert out == b"X" and rec["box"] is None and files is None
