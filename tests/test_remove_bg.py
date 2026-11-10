"""Remove BG: the cut-out engine (numpy + Pillow) and, when Qt is available, the tool window."""
import os
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFilter

from dragshift import cutout


def product_shot(size=(900, 700)) -> Image.Image:
    """Red mug with a handle on a light, slightly graded studio backdrop with a soft shadow."""
    w, h = size
    yy, xx = np.mgrid[0:h, 0:w]
    bg = np.stack([235 - 30 * yy / h, 238 - 25 * yy / h, 245 - 20 * yy / h], -1)
    rng = np.random.default_rng(0)
    im = Image.fromarray(np.clip(bg + rng.normal(0, 2, bg.shape), 0, 255).astype(np.uint8))
    d = ImageDraw.Draw(im)
    d.ellipse((300, 560, 600, 600), fill=(195, 195, 205))          # shadow
    im = im.filter(ImageFilter.GaussianBlur(1))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((330, 180, 560, 580), 30, fill=(200, 40, 50))
    d.ellipse((520, 260, 660, 460), fill=(200, 40, 50))
    d.ellipse((555, 300, 625, 420), fill=tuple(int(v) for v in bg[360, 590]))   # hole in the handle
    return im


def alpha(im: Image.Image) -> np.ndarray:
    return np.asarray(im.split()[3])


def test_product_shot_cutout():
    im = product_shot()
    out = cutout.remove_background(im)
    a = alpha(out)
    assert out.mode == "RGBA" and out.size == im.size
    assert a[350, 440] == 255, "mug body stays"
    assert a[20, 20] == 0 and a[680, 880] == 0, "backdrop is cleared"
    assert a[585, 450] == 0, "shadow on the backdrop is cleared"
    assert a[360, 590] == 255, "enclosed handle hole is kept by default"
    # no background tint left on the mug's edge
    edge = np.asarray(out)[350, 326:334]
    assert all(px[0] > px[2] for px in edge if px[3] > 60)


def test_enclosed_areas_option():
    out = cutout.remove_background(product_shot(), cutout.Cutout(holes=True))
    assert alpha(out)[360, 590] == 0


def test_subject_cut_off_at_bottom_is_kept():
    """A torso that runs off the bottom and sides of the frame isn't mistaken for background."""
    im = Image.new("RGB", (600, 750), (120, 160, 200))
    d = ImageDraw.Draw(im)
    d.ellipse((225, 150, 375, 350), fill=(225, 180, 150))
    d.rectangle((0, 525, 600, 750), fill=(60, 60, 70))
    d.ellipse((75, 400, 525, 700), fill=(60, 60, 70))
    out = cutout.remove_background(im.filter(ImageFilter.GaussianBlur(2)))
    a = alpha(out)
    assert a[700, 300] == 255 and a[700, 10] == 255, "torso stays"
    assert a[250, 300] == 255, "face stays"
    assert a[30, 30] == 0 and a[300, 560] == 0, "wall is cleared"


def test_click_seed_removes_an_enclosed_region():
    im = Image.new("RGB", (400, 300), "white")
    d = ImageDraw.Draw(im)
    d.rectangle((50, 50, 350, 250), fill=(20, 60, 200))
    d.rectangle((150, 110, 250, 190), fill=(250, 250, 250))      # window inside the subject
    assert alpha(cutout.remove_background(im))[150, 200] == 255
    out = cutout.remove_background(im, cutout.Cutout(seeds=[(0.5, 0.5)]))
    assert alpha(out)[150, 200] == 0
    assert alpha(out)[80, 80] == 255


def test_brushes_and_order():
    im = product_shot()
    erase = cutout.Stroke("erase", 0.03, [(0.45, 0.4), (0.55, 0.4)])
    out = cutout.remove_background(im, cutout.Cutout(strokes=[erase]))
    assert alpha(out)[280, 445] == 0, "erase clears the subject"
    keep = cutout.Stroke("keep", 0.03, [(0.05, 0.05)])
    out = cutout.remove_background(im, cutout.Cutout(strokes=[erase, keep]))
    a = alpha(out)
    assert a[35, 45] == 255, "keep restores the original pixels"
    assert np.allclose(np.asarray(out)[35, 45, :3], np.asarray(im)[35, 45], atol=1)
    assert a[280, 445] == 0


def test_trim_crops_to_subject():
    out = cutout.remove_background(product_shot(), cutout.Cutout(trim=True))
    w, h = out.size
    assert 300 < w < 420 and 380 < h < 460


def test_preview_and_save_resolutions_agree():
    im = product_shot()
    full = cutout.remove_background(im)
    small = im.resize((450, 350))
    prev = cutout.remove_background(small, work_side=cutout.PREVIEW_WORK_SIDE)
    a = np.asarray(full.split()[3].resize((450, 350))) > 127
    b = alpha(prev) > 127
    assert (a == b).mean() > 0.98


@pytest.mark.parametrize("mode", ["RGB", "RGBA", "L", "P"])
def test_odd_inputs(mode):
    for size in ((1, 1), (3, 2), (64, 48)):
        im = Image.new("RGB", size, (255, 100, 80)).convert(mode)
        out = cutout.remove_background(im, cutout.Cutout(seeds=[(0.5, 0.5)]))
        assert out.size == size and out.mode == "RGBA"


def test_already_transparent_input():
    im = Image.new("RGBA", (300, 200), (0, 0, 0, 0))
    ImageDraw.Draw(im).ellipse((60, 40, 240, 160), fill=(10, 10, 10, 255))   # black on transparent
    a = alpha(cutout.remove_background(im))
    assert a[100, 150] == 255 and a[5, 5] == 0


def test_regions_connectivity():
    m = np.zeros((5, 5), bool)
    m[0, 0] = m[1, 1] = True                 # diagonal pair
    assert cutout.Regions(m).n == 2
    assert cutout.Regions(m, eight=True).n == 1
    r = cutout.Regions(np.ones((4, 6), bool))
    assert r.n == 1 and r.area[0] == 24 and r.paint(r.touching_border()).all()


def test_report_summary(tmp_path):
    from dragshift import report
    src = tmp_path / "Mug.jpg"
    product_shot().save(src)
    out = tmp_path / "Mug No Background.png"
    cutout.remove_background(Image.open(src)).save(out)
    r = report.summarize("tool:Remove BG", [str(src)], [out])
    assert r.title == "Background removed"
    assert r.good.endswith("% transparent")


# ---------------------------------------------------------------- the window (needs Qt)
@pytest.fixture(scope="module")
def qapp():
    pytest.importorskip("PySide6")
    pytest.importorskip("fitz")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_remove_bg_window(qapp, tmp_path):
    from dragshift import tools
    from dragshift.app import IMAGE_TOOLS, GLYPHS, HINTS
    assert "Remove BG" in IMAGE_TOOLS and "Remove BG" in GLYPHS and "Remove BG" in HINTS
    src = tmp_path / "Mug.jpg"
    product_shot().save(src, quality=92)
    done = []
    w = tools.RemoveBgWindow(str(src), lambda *a, **k: done.append(a))
    assert w._cut is not None and cutout.removed_share(w._cut) > 0.5
    w.add_seed((0.6, 0.5))
    w.add_stroke(cutout.Stroke("erase", 0.02, [(0.1, 0.1), (0.2, 0.2)]))
    assert w.history == ["seed", "stroke"]
    w.undo()
    assert w.history == ["seed"] and not w.params.strokes
    w._compare(True)
    w._compare(False)
    w.bd_grp.button(2).click()
    w.trim.setChecked(True)
    w.save()                                   # no app: runs synchronously and notifies
    assert done and done[0][0] == "tool:Remove BG"
    out = Path(done[0][2][0])
    assert out.name == "Mug No Background.png" and out.exists()
    with Image.open(out) as im:
        assert im.mode == "RGBA" and im.width < 900
