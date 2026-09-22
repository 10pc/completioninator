"""Compositor unit tests: pure planning math, no ffmpeg needed."""

from pathlib import Path

from osu_pipeline import compositor


def _clip(i, duration, day="2026-09-06"):
    return compositor.Clip(id=i, path=Path(f"{i}.mp4"), day=day,
                           duration=duration, has_audio=True)


def test_placed_graph_uses_explicit_rects():
    clips = [_clip(1, 10.0), _clip(2, 10.0)]
    items = [(clips[0], compositor.Rect(10, 90, 320, 180)),
             (clips[1], compositor.Rect(340, 90, 320, 180))]
    script = compositor.build_placed_graph(
        items, 1920, 1000, 80, 30, "H", "font.ttf", 36)
    assert "xstack=inputs=2:layout=10_90|340_90" in script
    assert "scale=320:180" in script
    # single tile takes the pad path, no xstack
    solo = compositor.build_placed_graph(
        items[:1], 1920, 1000, 80, 30, "H", "font.ttf", 36)
    assert "xstack" not in solo and "pad=1920:1080:10:90" in solo


def test_morph_layouts_preserve_order_and_endpoints():
    clips = {1: _clip(1, 40.0), 2: _clip(2, 40.0)}
    span = compositor.MorphSpan(start=30.0, end=31.0, tiles=[
        compositor.MorphTile(1, 0, 80, 100, 60, 10, 90, 200, 120, False),
        compositor.MorphTile(2, 100, 80, 100, 60, 210, 90, 200, 120, True),
    ])
    items_a, items_b = compositor.morph_layouts(span, clips)
    assert [c.id for c, _r in items_a] == [1, 2]
    assert items_a[0][1] == compositor.Rect(0, 80, 100, 60)
    assert items_b[0][1] == compositor.Rect(10, 90, 200, 120)


def test_still_and_dissolve_commands(tmp_path: Path, monkeypatch):
    import subprocess

    seen = {}

    def _fake_run(cmd, **kwargs):
        seen.setdefault("cmds", []).append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", _fake_run)
    clips = [_clip(1, 40.0)]
    items = [(clips[0], compositor.Rect(0, 80, 320, 180))]
    graph = tmp_path / "g.txt"
    graph.write_text("graph")
    compositor.encode_still("ffmpeg", items, graph, tmp_path / "a.png", 30.0)
    cmd = seen["cmds"][0]
    assert cmd[cmd.index("-i") - 2:cmd.index("-i")] == ["-ss", "30.000"]
    assert "1" in cmd[cmd.index("-frames:v") + 1:cmd.index("-frames:v") + 2]
    compositor.encode_dissolve("ffmpeg", tmp_path / "a.png", tmp_path / "b.png",
                               1.0, 30, tmp_path / "seg.mp4", "veryfast", 23)
    dcmd = " ".join(seen["cmds"][1])
    assert "xfade=transition=fade:duration=1.000:offset=0" in dcmd
    assert "-loop" in seen["cmds"][1]


def test_seg_still_first_and_last(tmp_path: Path, monkeypatch):
    import subprocess

    seen = {}

    def _fake_run(cmd, **kwargs):
        seen.setdefault("cmds", []).append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", _fake_run)
    seg = tmp_path / "seg.mp4"
    seg.write_bytes(b"\x00" * 16)
    compositor.encode_seg_still("ffmpeg", seg, tmp_path / "first.png", last=False)
    compositor.encode_seg_still("ffmpeg", seg, tmp_path / "last.png", last=True)
    first, last_cmd = seen["cmds"]
    assert "-sseof" not in first and first[first.index("-i") + 1] == str(seg)
    assert "-sseof" in last_cmd and last_cmd[last_cmd.index("-sseof") + 1] == "-0.1"
    assert last_cmd[last_cmd.index("-frames:v") + 1] == "1"


def test_probe_clip_reports_dimensions(tmp_path: Path, monkeypatch):
    import json
    import subprocess

    payload = {"format": {"duration": "42.5"},
               "streams": [{"codec_type": "video", "width": 1920, "height": 1080},
                           {"codec_type": "audio"}]}

    def _fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(payload))

    monkeypatch.setattr(subprocess, "run", _fake_run)
    clip = compositor.probe_clip("ffprobe", tmp_path / "v.mp4")
    assert (clip.duration, clip.width, clip.height, clip.has_audio) == (42.5, 1920, 1080, True)


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


def test_rate_for_mods():
    assert compositor.rate_for_mods(0) == 1.0
    assert compositor.rate_for_mods(2) == 1.0  # Easy
    assert compositor.rate_for_mods(64) == 1.5  # DT
    assert compositor.rate_for_mods(66) == 1.5  # EZ+DT
    assert compositor.rate_for_mods(512) == 1.5  # NC
    assert compositor.rate_for_mods(256) == 0.75  # HT


def test_split_seek_delay():
    assert compositor.split_seek_delay(8000.0, 1.5) == (8.0, 0.0)
    assert compositor.split_seek_delay(-4469.0, 1.5) == (0.0, 4469.0 / 1000.0 / 1.5)
    assert compositor.split_seek_delay(0.0, 1.0) == (0.0, 0.0)


def test_audio_graph_applies_tempo_per_track():
    plain = [_clip(1, 100.0)]
    script, _ = compositor.build_audio_graph(plain, 100.0, 106.0)
    assert "atempo" not in script
    fast = [_clip(1, 100.0), _clip(2, 60.0)]
    fast[0].audio_rate = 1.5
    script, _ = compositor.build_audio_graph(fast, 100.0, 106.0)
    assert "[0:a]atempo=1.5,aresample=48000" in script
    assert "[1:a]aresample=48000" in script  # 1x track untouched
    assert compositor.atempo_chain(1.0) == ""
    assert compositor.atempo_chain(0.75) == "atempo=0.75,"


def test_audio_graph_delays_track_past_lead_in():
    clips = [_clip(1, 100.0)]
    script, _ = compositor.build_audio_graph(clips, 100.0, 106.0)
    assert "adelay" not in script
    clips[0].audio_delay = 4.469
    clips[0].audio_rate = 1.5
    script, _ = compositor.build_audio_graph(clips, 100.0, 106.0)
    # tempo first, delay after asetpts (which would reset its shift)
    assert "[0:a]atempo=1.5,aresample=48000,asetpts=PTS-STARTPTS,adelay=delays=4469:all=1[a0]" in script


def test_audio_graph_seeks_in_filter_not_input(tmp_path, monkeypatch):
    import subprocess

    clips = [_clip(1, 100.0)]
    clips[0].audio_offset = 21.575
    script, _ = compositor.build_audio_graph(clips, 100.0, 106.0)
    assert "[0:a]atrim=start=21.575,aresample=48000" in script

    seen = {}

    def _fake_run(cmd, **kwargs):
        seen["cmd"] = list(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", _fake_run)
    graph = tmp_path / "g.txt"
    graph.write_text("graph")
    compositor.encode_audio_mix("ffmpeg", clips, graph, tmp_path / "a.m4a", 60)
    assert "-ss" not in seen["cmd"]  # seeks live in-filter (VBR-safe)


def test_audio_graph_levels_grid_only():
    clips = [_clip(1, 100.0)]
    script, _ = compositor.build_audio_graph(clips, 100.0, 106.0)
    assert "loudnorm" not in script
    script, _ = compositor.build_audio_graph(clips, 100.0, 106.0, level_tracks=True)
    assert "[0:a]aresample=48000,asetpts=PTS-STARTPTS,loudnorm=I=-16:TP=-1.5:LRA=11,aresample=48000[a0]" in script


def test_audio_graph_fade_in():
    clips = [_clip(1, 100.0)]
    script, _ = compositor.build_audio_graph(clips, 100.0, 106.0)
    assert "afade=t=in" not in script
    script, _ = compositor.build_audio_graph(clips, 100.0, 106.0, fade_in=2.0)
    assert "[amix]afade=t=in:st=0:d=2.000,aformat" in script


def test_audio_graph_hits_bed_joins_mix(tmp_path):
    clips = [_clip(1, 100.0)]
    hits = tmp_path / "hits.m4a"
    hits.write_bytes(b"hits")
    script, ok = compositor.build_audio_graph(
        clips, 100.0, 106.0, hits_path=hits)
    assert ok is True
    assert "[1:a]aresample=48000,asetpts=PTS-STARTPTS[a1]" in script
    assert "amix=inputs=2" in script
    # hits-only bed still mixes (no voiced clips at all)
    mute = [_clip(1, 10.0)]
    mute[0].has_audio = False
    script, ok = compositor.build_audio_graph(
        mute, 10.0, 16.0, hits_path=hits)
    assert ok is True and "amix=inputs=1" not in script and "[aout]" in script


def test_audio_mix_appends_hits_input(tmp_path, monkeypatch):
    import subprocess

    seen = {}

    def _fake_run(cmd, **kwargs):
        seen.setdefault("cmds", []).append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", _fake_run)
    clips = [_clip(1, 40.0)]
    graph = tmp_path / "g.txt"
    graph.write_text("graph")
    hits = tmp_path / "hits.m4a"
    hits.write_bytes(b"hits")
    compositor.encode_audio_mix("ffmpeg", clips, graph, tmp_path / "a.m4a",
                                60, hits_path=hits)
    cmd = seen["cmds"][0]
    assert cmd[-3:] == ["-ar", "48000", str(tmp_path / "a.m4a")]
    inputs = [cmd[i + 1] for i, c in enumerate(cmd) if c == "-i"]
    assert inputs[-1] == str(hits)  # hits bed appends after music tracks


def test_span_hits_retimed_exact(tmp_path, monkeypatch):
    import subprocess

    seen = {}

    def _fake_run(cmd, **kwargs):
        seen.setdefault("cmds", []).append(list(cmd))
        for i, c in enumerate(cmd[:-1]):
            if c == "-t" and cmd[i + 1] == "61.500":
                pass
        out = Path(cmd[-1])
        if str(out).endswith(".m4a"):
            out.write_bytes(b"seg")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", _fake_run)
    grid = tmp_path / "grid"
    grid.mkdir()
    (grid / "seg-000.audio.mp4").write_bytes(b"audio")
    compositor.concat_span_hits("ffmpeg", grid, ["seg-000", "seg-001"],
                                {"seg-000": 61.5, "seg-001": 2.0},
                                tmp_path / "hits.m4a", tmp_path)
    per_span = seen["cmds"][0]
    # found audio is re-timed to the exact plan length (kills AAC priming
    # accumulation across dozens of spans)
    assert "-t" in per_span and "61.500" in per_span
    assert "apad=whole_dur=61.500" in " ".join(per_span)
    synth = seen["cmds"][1]
    assert "anullsrc" in " ".join(synth) and "2.000" in synth
    final = seen["cmds"][2]
    assert "-f" in final and "concat" in final
    # final join re-encodes: copy-concat would stack per-file AAC priming
    assert "-c" not in final or "copy" not in final
    assert "aac" in final


def test_audio_mix_uses_longest_n_tracks():
    clips = [_clip(i, float(10 + i)) for i in range(12)]  # 10s..21s
    picked = compositor.select_mix_clips(clips, 10)
    assert [c.duration for c in picked] == sorted(
        (c.duration for c in clips), reverse=True)[:10]
    script, ok = compositor.build_audio_graph(clips, 21.0, 27.0, max_tracks=10)
    assert ok is True
    assert "amix=inputs=10" in script
    assert "normalize=0" in script  # original volume kept
    assert "[10:a]" not in script  # only 10 inputs wired
    # opt-out mixes everything, as before
    script_all, _ = compositor.build_audio_graph(clips, 21.0, 27.0, max_tracks=0)
    assert "amix=inputs=12" in script_all


def test_outro_graph_text_fades():
    graph = compositor.build_outro_graph(1920, 1080, 6.0, "1,133/147,163", "0.73%",
                                         "/font.ttf", 72, 54)
    assert "color=black" in graph
    assert "1\\,133/147\\,163" in graph and "0.73%" in graph  # drawtext escaping
    assert "expansion=none" in graph  # bare % must render, not "Stray %" away
    assert "fade=t=in:st=0:d=1.000" in graph
    assert "fade=t=out:st=5.000:d=1.000" in graph
    assert "alpha=1" not in graph  # plain luma fades: alpha-out is a no-op upstream
    assert graph.rstrip().endswith("[vout]")


def test_clip_end_times_and_gate():
    a = _clip(1, 60.0)
    b = _clip(2, 30.0)
    tl = compositor.plan_timeline([a, b], morph_s=1.0, quant=0.5)
    ends = compositor.clip_end_times(tl)
    assert ends[2] <= 30.0 and ends[1] >= ends[2]
    b.audio_end = ends[2]
    script, _ = compositor.build_audio_graph([a, b], 60.0, 66.0)
    assert "volume=enable='lte(t,%.3f)':volume=0" % ends[2] in script
    script, _ = compositor.build_audio_graph([_clip(1, 60.0)], 60.0, 66.0)
    assert "volume=enable" not in script


def test_single_clip_skips_xstack():
    seg = compositor.Segment(start=0.0, end=60.0, active=[_clip(1, 60.0)])
    graph = compositor.build_segment_graph(
        seg, 1920, 1000, 80, 30, "HDR", "/font.ttf", 36)
    assert "xstack" not in graph
    assert "[0:v]scale=1760:990,setsar=1" in graph  # k=1: exact 16:9, centered
    # absolute canvas coords: tile sits below the header, not under it
    assert "pad=1920:1080:80:85:black[vgrid]" in graph
    assert "fade=" not in graph
