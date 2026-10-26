import json
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pytest

from talklog import config, render, transcribe
from talklog.recorder import FRAME_BYTES, FRAME_S, Segmenter


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'CONFIG_DIR', tmp_path / 'conf')
    return replace(config.load(), data_dir=tmp_path / 'data', silence_end=10, max_segment=60, min_speech=1)


class FakeWriter:
    def __init__(self, start, log):
        self.start, self.frames, self.log = start, 0, log
    def write(self, pcm): self.frames += 1
    def close(self, keep, duration, speech): self.log.append((self.start, keep, round(duration, 2), round(speech, 2)))


def feed(seg, t0, pattern):
    """pattern: [(秒数, 是否人声), ...]，从 t0 开始逐帧喂。返回结束时的钟点。"""
    t = t0
    for secs, voiced in pattern:
        for _ in range(round(secs / FRAME_S)):
            seg.feed(b'\0' * FRAME_BYTES, voiced, t)
            t += timedelta(seconds=FRAME_S)
    return t


def make(cfg):
    closed = []
    return Segmenter(cfg, lambda start: FakeWriter(start, closed)), closed


T0 = datetime(2026, 10, 27, 9, 30)


def test_short_noise_does_not_open(cfg):
    seg, closed = make(cfg)
    feed(seg, T0, [(5, False), (0.3, True), (5, False), (0.2, True), (30, False)])
    assert not seg.recording and closed == []


def test_conversation_opens_with_preroll_and_closes_after_silence(cfg):
    seg, closed = make(cfg)
    feed(seg, T0, [(5, False), (4, True), (2, False), (3, True), (11, False)])
    assert not seg.recording
    (start, keep, dur, speech), = closed
    assert keep
    # 人声从第 5 秒开始，累计 0.6 秒时开段，往前补 3 秒：段从 2.6 秒左右开始
    assert abs((start - T0).total_seconds() - 2.6) < 0.1
    assert speech == pytest.approx(7, abs=0.1)
    # 最后一次有人声在第 14 秒，再过 10 秒切：段长约 24 - 2.6
    assert dur == pytest.approx(24 - 2.6, abs=1.1)


def test_pause_shorter_than_silence_end_stays_in_one_segment(cfg):
    seg, closed = make(cfg)
    feed(seg, T0, [(2, True), (8, False), (2, True), (8, False), (2, True), (12, False)])
    assert len(closed) == 1 and closed[0][3] == pytest.approx(6, abs=0.1)


def test_long_conversation_rolls_over_at_max_segment(cfg):
    seg, closed = make(cfg)
    end = feed(seg, T0, [(1, True), (1, False)] * 50)     # 100 秒一直在说
    assert len(closed) == 1 and closed[0][2] == pytest.approx(60, abs=0.1)
    assert seg.recording and seg.seg_start == closed[0][0] + timedelta(seconds=60)
    seg.close()
    assert len(closed) == 2


def test_too_little_speech_is_dropped(cfg):
    seg, closed = make(replace(cfg, min_speech=3))
    feed(seg, T0, [(1, True), (12, False)])
    assert closed and closed[0][1] is False


def test_window(cfg):
    c = replace(cfg, days=(1, 2, 3, 4, 5), start=time(9), end=time(19))
    assert c.in_window(datetime(2026, 10, 27, 9, 0))          # 周二
    assert not c.in_window(datetime(2026, 10, 27, 19, 0))
    assert not c.in_window(datetime(2026, 10, 25, 10, 0))     # 周日


def test_config_file_overrides(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'CONFIG_DIR', tmp_path)
    (tmp_path / 'config.toml').write_text('[record]\nstart = "08:30"\ndays = [1, 3]\n[storage]\nkeep_audio_days = 3\n')
    c = config.load()
    assert c.start == time(8, 30) and c.days == (1, 3) and c.keep_audio_days == 3 and c.end == time(19)


RESULT = {'transcripts': [{'channel_id': 0, 'sentences': [
    {'begin_time': 1000, 'end_time': 2500, 'text': '今天先对一下进度。', 'speaker_id': 0},
    {'begin_time': 2600, 'end_time': 4000, 'text': '上周那个版本发了吗？', 'speaker_id': 0},
    {'begin_time': 4500, 'end_time': 5200, 'text': '发了。', 'speaker_id': 1},
    {'begin_time': 66000, 'end_time': 67000, 'text': '好。', 'speaker_id': 0},
]}]}


def test_turns_merge_same_speaker():
    ts = render.turns(RESULT)
    assert [(t['speaker'], t['text']) for t in ts] == [(0, '今天先对一下进度。上周那个版本发了吗？'), (1, '发了。'), (0, '好。')]


def test_write_day(cfg):
    d = cfg.raw_dir / '2026-10-27'
    d.mkdir(parents=True)
    meta = {'start': '2026-10-27T09:42:10', 'duration': 600, 'speech': 120}
    (d / '094210.json').write_text(json.dumps({'meta': meta, 'state': 'ok', 'result': RESULT}))
    (d / '110000.json').write_text(json.dumps({'meta': {**meta, 'start': '2026-10-27T11:00:00'}, 'state': 'no_speech', 'result': None}))
    md = render.write_day(cfg, '2026-10-27').read_text()
    assert '1 场对话，录到人声约 2 分钟' in md
    assert '## 09:42–09:52（10 分钟，2 位说话人）' in md
    assert '- **09:42:11 说话人1**：今天先对一下进度。上周那个版本发了吗？' in md
    assert '- **09:42:14 说话人2**：发了。' in md
    assert '- **09:43:16 说话人1**：好。' in md
    assert '## 11:00–11:10（10 分钟，没识别出话）' in md


def _seg(cfg, day, hms):
    d = cfg.audio_dir / day
    d.mkdir(parents=True, exist_ok=True)
    (d / f'{hms}.ogg').write_bytes(b'x')
    (d / f'{hms}.json').write_text(json.dumps({'start': f'{day}T09:00:00', 'duration': 60, 'speech': 10}))
    return d / f'{hms}.ogg'


def test_pending_skips_unfinished_and_done(cfg):
    a = _seg(cfg, '2026-10-27', '090000')
    b = _seg(cfg, '2026-10-27', '100000')
    (cfg.audio_dir / '2026-10-27' / '110000.ogg.part').write_bytes(b'x')
    transcribe._save(cfg, a, 'ok', RESULT)
    assert transcribe.pending(cfg) == [b]


def test_failed_retries_then_gives_up(cfg):
    a = _seg(cfg, '2026-10-27', '090000')
    for _ in range(transcribe.MAX_TRIES - 1):
        transcribe._save(cfg, a, 'failed', 'BOOM')
        assert transcribe.pending(cfg) == [a]
    transcribe._save(cfg, a, 'failed', 'BOOM')
    assert transcribe.pending(cfg) == []
    assert json.loads(transcribe.raw_path(cfg, a).read_text())['error'] == 'BOOM'


def test_cleanup_keeps_recent_days(cfg):
    for day in ('2026-10-18', '2026-10-20', '2026-10-27'):
        _seg(cfg, day, '090000')
    transcribe.cleanup(cfg, date(2026, 10, 27))
    assert sorted(p.name for p in cfg.audio_dir.iterdir()) == ['2026-10-20', '2026-10-27']
