"""配置：~/.config/talk-log/config.toml（可选，没写的用默认值）和 ~/.config/talk-log/env（密钥，必需）。"""
import os, tomllib
from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path

CONFIG_DIR = Path(os.environ.get('TALKLOG_CONFIG_DIR', '~/.config/talk-log')).expanduser()
LOG_DIR = Path('~/Library/Logs/talk-log').expanduser()


@dataclass(frozen=True)
class Config:
    device: str                  # avfoundation 的输入设备名，固定用内置麦克风：跟着系统默认走会在连上 AirPods 时去开耳机的麦，耳机就掉成通话音质
    days: tuple[int, ...]        # 录音的星期几，1 是周一
    start: time
    end: time
    silence_end: float           # 连续这么多秒没人说话，这场对话就算结束、切段
    max_segment: float           # 一段最长秒数，到了接着录下一段（百炼说话人分离建议单段不超过 2 小时）
    min_speech: float            # 一段里有人声的秒数不到这个就丢掉，多半是咳嗽、关门这类
    data_dir: Path
    keep_audio_days: int
    model: str

    @property
    def audio_dir(self) -> Path: return self.data_dir / 'audio'
    @property
    def raw_dir(self) -> Path: return self.data_dir / 'raw'
    @property
    def transcripts_dir(self) -> Path: return self.data_dir / 'transcripts'

    def in_window(self, now: datetime) -> bool:
        return now.isoweekday() in self.days and self.start <= now.time() < self.end


def _hm(s: str) -> time:
    h, m = s.split(':'); return time(int(h), int(m))


def load() -> Config:
    f = CONFIG_DIR / 'config.toml'
    d = tomllib.loads(f.read_text()) if f.exists() else {}
    r, s = d.get('record', {}), d.get('storage', {})
    return Config(
        device=r.get('device', 'MacBook Air麦克风'),
        days=tuple(r.get('days', [1, 2, 3, 4, 5])),
        start=_hm(r.get('start', '09:00')), end=_hm(r.get('end', '19:00')),
        silence_end=float(r.get('silence_end_seconds', 180)),
        max_segment=float(r.get('max_segment_minutes', 60)) * 60,
        min_speech=float(r.get('min_speech_seconds', 3)),
        data_dir=Path(s.get('data_dir', '~/.local/share/talk-log')).expanduser(),
        keep_audio_days=int(s.get('keep_audio_days', 7)),
        model=d.get('asr', {}).get('model', 'qwen-audio-3.1-asr-flash-filetrans'),
    )


def secrets() -> dict[str, str]:
    """~/.config/talk-log/env：DASHSCOPE_API_KEY、OSS_ACCESS_KEY_ID、OSS_ACCESS_KEY_SECRET、OSS_BUCKET、OSS_ENDPOINT。"""
    f, kv = CONFIG_DIR / 'env', {}
    if f.exists():
        for line in f.read_text().splitlines():
            if '=' in line and not line.lstrip().startswith('#'):
                k, v = line.split('=', 1); kv[k.strip()] = v.strip().strip('"\'')
    return kv
