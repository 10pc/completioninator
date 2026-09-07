"""Compositor unit tests: pure planning math, no ffmpeg needed."""

from pathlib import Path

from osu_pipeline import compositor


def _clip(i, duration, day="2026-09-06"):
    return compositor.Clip(id=i, path=Path(f"{i}.mp4"), day=day,
                           duration=duration, has_audio=True)


def test_grid_dims():
    assert compositor.grid_dims(1) == (1, 1)
    assert compositor.grid_dims(4) == (3, 2)  # round(sqrt(4*1.92))=3
    cols, rows = compositor.grid_dims(12)
    assert cols * rows >= 12 and rows <= cols + 1
    cols, rows = compositor.grid_dims(300)
    assert cols * rows >= 300
    try:
        compositor.grid_dims(0)
    except ValueError:
        pass
    else:
        raise AssertionError("grid_dims(0) should raise")


def test_tile_boxes_are_169_centered_and_bounded():
    for k in (1, 2, 3, 5, 12, 21, 300):
        cols, rows = compositor.grid_dims(k)
        tw, th = compositor.tile_size(cols, rows, 1920, 1000)
        assert tw % 2 == 0 and th % 2 == 0
        assert tw * 9 == th * 16, (k, tw, th)  # exactly 16:9
        rects = compositor.layout_rects(cols, rows, k, tw, th, 1920, 1000, 80)
        assert len(rects) == k
        for r in rects:
            assert r.w * 9 == r.h * 16  # aspect preserved: lerp stays exact
            assert r.x >= 0 and r.y >= 80
            assert r.x + r.w <= 1920 and r.y + r.h <= 1080
        # block is centered: left and right margins differ by at most a tile
        xs = [r.x for r in rects if r.y == rects[0].y]
        assert min(xs) == (1920 - len(xs) * tw) // 2


def test_tile_size_stays_even():
    for k in (1, 2, 3, 5, 12, 21, 300):
        cols, rows = compositor.grid_dims(k)
        tw, th = compositor.tile_size(cols, rows, 1920, 1000)
        assert tw % 2 == 0 and th % 2 == 0, (k, cols, rows, tw, th)
        assert cols * tw <= 1920 and 80 + rows * th <= 1080


def test_plan_timeline_static_and_morphs():
    clips = [_clip(1, 100.0), _clip(2, 60.0), _clip(3, 30.0)]
    spans = compositor.plan_timeline(clips, morph_s=1.0)
    kinds = [type(s).__name__ for s in spans]
    # morphs occupy each finish's final second; the rest is static
    assert kinds == ["Segment", "MorphSpan", "Segment", "MorphSpan", "Segment"], kinds
    assert [(s.start, s.end) for s in spans] == [
        (0.0, 29.0), (29.0, 30.0), (30.0, 59.0), (59.0, 60.0), (60.0, 100.0)]
    total = sum(s.length for s in spans)
    assert abs(total - 100.0) < 1e-9
    # first morph: all 3 alive, clip 3 dying
    morph = spans[1]
    assert len(morph.tiles) == 3
    dying = [t for t in morph.tiles if t.dying]
    assert len(dying) == 1 and dying[0].clip_id == 3
    assert (dying[0].bw, dying[0].bh) == (16, 9)
    # final static span holds the longest clip alone
    assert [c.id for c in spans[-1].active] == [1]


def test_plan_timeline_tiny_gap_degrades_to_cut():
    # With exact boundaries, a 0.1s gap is a hard cut, not a morph.
    clips = [_clip(1, 100.0), _clip(2, 30.0), _clip(3, 30.1)]
    spans = compositor.plan_timeline(clips, morph_s=1.0, min_morph=0.25, quant=0)
    kinds = [type(s).__name__ for s in spans]
    assert kinds == ["Segment", "MorphSpan", "Segment", "Segment"], kinds
    total = sum(s.length for s in spans)
    assert abs(total - 100.0) < 1e-9


def test_plan_timeline_quantize_collapses_float_dust():
    clips = [_clip(1, 100.0), _clip(2, 30.0), _clip(3, 30.000001)]
    spans = compositor.plan_timeline(clips, morph_s=1.0, quant=0.5)
    kinds = [type(s).__name__ for s in spans]
    assert kinds == ["Segment", "MorphSpan", "Segment"], kinds
    for s in spans:
        assert s.length >= 0.5  # no sliver ffmpeg would reject
    total = sum(s.length for s in spans)
    assert abs(total - 100.0) < 1e-9
    # quant=0 keeps exact boundaries (fragile, opt-in)
    exact = compositor.plan_timeline(clips, morph_s=1.0, quant=0)
    assert len(exact) > len(spans)


def test_plan_timeline_single_clip_no_morph():
    spans = compositor.plan_timeline([_clip(1, 50.0)])
    assert len(spans) == 1 and isinstance(spans[0], compositor.Segment)


def test_morph_graph_interpolates_and_fades_dying():
    clips = [_clip(1, 100.0), _clip(2, 60.0), _clip(3, 30.0)]
    spans = compositor.plan_timeline(clips, morph_s=1.0)
    morph = spans[1]
    by_id = {c.id: c for c in clips}
    script, ordered = compositor.build_morph_graph(
        morph, by_id, 1920, 1000, 80, 30, "HDR", "/font.ttf", 36)
    assert [c.id for c in ordered] == [1, 2, 3]
    assert "color=black" in script
    assert "eval=frame" in script
    assert "min(max(t/1.0" in script  # lerp over the morph duration
    assert "fade=t=out:st=0:d=1.000:alpha=1" in script  # dying tile
    assert script.count("overlay=") == 3


def test_header_text():
    assert compositor.header_text("2026-09-06", 12, None) == "06-09-2026 | 12 maps"
    assert compositor.header_text("2026-09-06", 12, "2026-08-22..2026-09-04") == \
        "06-09-2026 | 12 maps"
    assert compositor.header_text("2026-09-06", 1, None, extra="crest 5.1") == \
        "06-09-2026 | 1 maps | crest 5.1"


def test_segment_graph_video_only_aspect():
    clips = [_clip(1, 100.0), _clip(2, 60.0, day="2026-09-05")]
    seg = compositor.Segment(start=0.0, end=60.0, active=clips)
    graph = compositor.build_segment_graph(
        seg, 1920, 1000, 80, 30, "HDR", "/font.ttf", 36)
    assert "xstack=inputs=2:layout=" in graph
    assert ":fill=black" in graph
    # plain scale into exact 16:9 boxes: no letterbox pad inside tiles
    assert "[0:v]scale=960:540,setsar=1" in graph
    assert "(ow-iw)/2" not in graph
    # no fades: morph spans own all transitions; no audio: separate mix
    assert "fade=" not in graph
    assert "aresample" not in graph and "[aout]" not in graph
    assert "drawtext=" in graph and "HDR" in graph
    assert "pad=1920:1080:0:0:black[vout]" in graph


def test_segment_graph_fade_out_param():
    seg = compositor.Segment(start=0.0, end=60.0, active=[_clip(1, 60.0)])
    plain = compositor.build_segment_graph(
        seg, 1920, 1000, 80, 30, "HDR", "/font.ttf", 36)
    assert "fade=" not in plain
    faded = compositor.build_segment_graph(
        seg, 1920, 1000, 80, 30, "HDR", "/font.ttf", 36, fade_out=1.0)
    assert "fade=t=out:st=59.000:d=1.000" in faded


def test_audio_graph_stacks_loud():
    clips = [_clip(1, 100.0), _clip(2, 60.0)]
    script, ok = compositor.build_audio_graph(clips, 100.0, 106.0)
    assert ok is True
    assert "amix=inputs=2" in script
    assert "normalize=0" in script  # loud stacked wall, not leveled static
    assert "alimiter" in script  # catches digital clipping only
    assert "[0:a]" in script and "[1:a]" in script
    assert "afade=t=out:st=98.000:d=2" in script
    assert "apad=whole_dur=106.000" in script


def test_audio_graph_single_and_none():
    script, ok = compositor.build_audio_graph([_clip(1, 100.0)], 100.0, 106.0)
    assert ok is True and "[aout]" in script and "amix=inputs=" not in script
    mute = [_clip(1, 10.0), _clip(2, 20.0)]
    for c in mute:
        c.has_audio = False
    assert compositor.build_audio_graph(mute, 20.0, 26.0) == ("", False)


def test_outro_graph_text_fades():
    graph = compositor.build_outro_graph(1920, 1080, 6.0, "1,133/147,163", "0.73%",
                                         "/font.ttf", 72, 54)
    assert "color=black" in graph
    assert "1\\,133/147\\,163" in graph and "0.73%" in graph  # drawtext escaping
    assert "fade=t=in:st=0:d=1.000" in graph
    assert "fade=t=out:st=5.000:d=1.000" in graph
    assert "alpha=1" not in graph  # plain luma fades: alpha-out is a no-op upstream
    assert graph.rstrip().endswith("[vout]")


def test_single_clip_skips_xstack():
    seg = compositor.Segment(start=0.0, end=60.0, active=[_clip(1, 60.0)])
    graph = compositor.build_segment_graph(
        seg, 1920, 1000, 80, 30, "HDR", "/font.ttf", 36)
    assert "xstack" not in graph
    assert "[0:v]scale=1760:990,setsar=1" in graph  # k=1: exact 16:9, centered
    # absolute canvas coords: tile sits below the header, not under it
    assert "pad=1920:1080:80:85:black[vgrid]" in graph
    assert "fade=" not in graph
