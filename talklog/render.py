"""每天一份 Markdown：transcripts/<日期>.md，由 raw/<日期>/ 下的转写记录整份重写，可以随时重跑。
每场对话（一段录音）一节；同一个说话人连着说的几句并成一行，行首是这一行开始的钟点。
说话人编号只在一场对话里有效：这一场的「说话人1」和下一场的「说话人1」不一定是同一个人。"""
import json
from datetime import datetime, timedelta
from pathlib import Path

from .config import Config


def turns(result: dict) -> list[dict]:
    """百炼结果 → [{start, speaker, text}]，start 是相对文件开头的秒数；同一说话人连着的句子并成一个。"""
    sents = sorted((s for tr in result.get('transcripts', []) for s in tr.get('sentences', []) if s.get('text', '').strip()),
                   key=lambda s: s['begin_time'])
    out = []
    for s in sents:
        spk = s.get('speaker_id')
        if out and out[-1]['speaker'] == spk:
            out[-1]['text'] += s['text'].strip()
        else:
            out.append({'start': s['begin_time'] / 1000, 'speaker': spk, 'text': s['text'].strip()})
    return out


def _who(spk) -> str:
    return f'说话人{spk + 1}' if isinstance(spk, int) else '说话人?'


def section(rec: dict) -> tuple[str, float]:
    """一场对话的 Markdown 和其中的人声秒数。"""
    meta = rec['meta']
    t0 = datetime.fromisoformat(meta['start'])
    t1 = t0 + timedelta(seconds=meta['duration'])
    head = f"## {t0:%H:%M}–{t1:%H:%M}"
    if rec['state'] == 'no_speech':
        return f"{head}（{meta['duration'] / 60:.0f} 分钟，没识别出话）\n", 0
    if rec['state'] == 'failed':
        return f"{head}（转写失败：{rec['error']}）\n", 0
    ts = turns(rec['result'])
    n = len({t['speaker'] for t in ts})
    lines = [f"{head}（{meta['duration'] / 60:.0f} 分钟，{n} 位说话人）", '']
    lines += [f"- **{t0 + timedelta(seconds=t['start']):%H:%M:%S} {_who(t['speaker'])}**：{t['text']}" for t in ts]
    return '\n'.join(lines) + '\n', meta['speech']


def write_day(cfg: Config, day: str) -> Path:
    recs = [json.loads(p.read_text()) for p in sorted((cfg.raw_dir / day).glob('*.json'))]
    parts, speech, talks = [], 0.0, 0
    for rec in recs:
        text, s = section(rec)
        parts.append(text)
        speech += s
        talks += rec['state'] == 'ok'
    body = f"# {day} 对话记录\n\n{talks} 场对话，录到人声约 {speech / 60:.0f} 分钟。说话人编号只在同一场里有效。\n\n" + '\n'.join(parts)
    cfg.transcripts_dir.mkdir(parents=True, exist_ok=True)
    p = cfg.transcripts_dir / f'{day}.md'
    p.write_text(body)
    return p
