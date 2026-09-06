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


def test_layout_string_positions():
    assert compositor.layout_string(2, 2, 3, 100, 50, 80) == "0_80|100_80|0_130"
    assert compositor.layout_string(1, 1, 1, 1920, 1000, 80) == "0_80"


def test_tile_size_stays_even():
    for k in (1, 2, 3, 5, 12, 21, 300):
        cols, rows = compositor.grid_dims(k)
        tw, th = compositor.tile_size(cols, rows, 1920, 1000)
        assert tw % 2 == 0 and th % 2 == 0, (k, cols, rows, tw, th)
        assert cols * tw <= 1920 and 80 + rows * th <= 1080


def test_plan_segments_shrinking_grid():
    clips = [_clip(1, 100.0), _clip(2, 60.0), _clip(3, 30.0)]
    segs = compositor.plan_segments(clips)
    assert [(s.start, s.end) for s in segs] == [(0.0, 30.0), (30.0, 60.0), (60.0, 100.0)]
    assert [len(s.active) for s in segs] == [3, 2, 1]
    assert segs[-1].active[0].id == 1  # longest survives alone
    # grid positions follow (day, id) order regardless of duration
    assert [c.id for c in segs[0].active] == [1, 2, 3]


def test_plan_segments_equal_durations():
    segs = compositor.plan_segments([_clip(1, 50.0), _clip(2, 50.0)])
    assert len(segs) == 1 and len(segs[0].active) == 2


def test_header_text():
    assert compositor.header_text("2026-09-06", 12, None) == \
        "OSU! COMPLETIONIST -- 2026-09-06 -- 12 MAPS"
    assert "2026-08-22..2026-09-04" in \
        compositor.header_text("2026-09-06", 12, "2026-08-22..2026-09-04", extra="crest 5.1")
    assert "crest" in compositor.header_text("2026-09-06", 1, None, extra="crest 5.1")


def test_segment_graph_references_longest_audio():
    clips = [_clip(1, 100.0), _clip(2, 60.0, day="2026-09-05")]
    seg = compositor.Segment(start=0.0, end=60.0, active=clips)
    li = compositor.longest_index(seg.active)
    assert li == 0
    graph, audio = compositor.build_segment_graph(
        seg, li, 1920, 1000, 80, 30, "HDR", "/font.ttf", 36)
    assert "xstack=inputs=2:layout=" in graph
    assert ":fill=black" in graph
    assert "[0:a]aresample=48000" in graph  # longest clip's audio
    assert audio == "[aout]"
    assert "drawtext=" in graph and "HDR" in graph


def test_single_clip_skips_xstack():
    seg = compositor.Segment(start=0.0, end=60.0, active=[_clip(1, 60.0)])
    graph, audio = compositor.build_segment_graph(
        seg, 0, 1920, 1000, 80, 30, "HDR", "/font.ttf", 36)
    assert "xstack" not in graph
    assert "[0:v]scale=1920:1000" in graph
    assert audio == "[aout]"
