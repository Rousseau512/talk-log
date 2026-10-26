"""录音：ffmpeg 从内置麦克风取 16k 单声道 PCM，webrtcvad 逐 30ms 判断有没有人声，按对话切段，每段编成 Opus 存进
audio/<日期>/<时分秒>.ogg，旁边一个同名 .json 记开始时间、时长、人声秒数。

切段规则（Segmenter，纯逻辑，测试里直接喂帧）：
- 没在录的时候，最近 2 秒里人声累计到 0.6 秒就开一段，往前补 3 秒，免得第一句话被截掉头；
- 录着的时候，连续 silence_end 秒没人声就结束这段；一段满 max_segment 秒接着开下一段；
- 一段里人声不到 min_speech 秒就整段丢掉。
中间的停顿照样写进文件，不剪掉：转写结果里每句的时间是相对文件开头的，剪了就对不上钟点。百炼只按人声时长计费，静音不花钱。

电脑睡眠再醒来，音频会断一截：按实际时钟和已读帧数对不上超过 20 秒来认，当前这段就此结束。"""
import collections, json, signal, subprocess, sys, time as _time
from datetime import datetime, timedelta
from pathlib import Path

from .config import Config, LOG_DIR

RATE, FRAME_MS = 16000, 30
FRAME_BYTES = RATE * FRAME_MS // 1000 * 2
FRAME_S = FRAME_MS / 1000
START_WINDOW, START_SPEECH = 2.0, 0.6     # 最近 2 秒里人声累计 0.6 秒就开一段
ACTIVE_WINDOW, ACTIVE_RATIO = 1.0, 0.3    # 最近 1 秒里三成的帧是人声，就算这一刻有人在说话
PREROLL = 3.0
GAP = 20.0


def log(msg: str) -> None:
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


class OpusWriter:
    """一段录音：PCM 灌进 ffmpeg 编成 Opus，先写 .part，结束时留下就改名成 .ogg 并写 .json，不留就删掉。"""

    def __init__(self, audio_dir: Path, start: datetime):
        d = audio_dir / f'{start:%Y-%m-%d}'
        d.mkdir(parents=True, exist_ok=True)
        self.start, self.path = start, d / f'{start:%H%M%S}.ogg'
        self.part = self.path.with_suffix('.ogg.part')
        self.proc = subprocess.Popen(
            ['ffmpeg', '-nostdin', '-hide_banner', '-v', 'error', '-y', '-f', 's16le', '-ar', str(RATE), '-ac', '1', '-i', '-',
             '-c:a', 'libopus', '-b:a', '24k', '-application', 'voip', '-f', 'ogg', str(self.part)],
            stdin=subprocess.PIPE)

    def write(self, pcm: bytes) -> None:
        self.proc.stdin.write(pcm)

    def close(self, keep: bool, duration: float, speech: float) -> None:
        self.proc.stdin.close()
        self.proc.wait(timeout=60)
        if keep and self.proc.returncode == 0 and self.part.exists():
            self.path.with_suffix('.json').write_text(json.dumps(
                {'start': self.start.isoformat(timespec='seconds'), 'duration': round(duration, 1), 'speech': round(speech, 1)}))
            self.part.rename(self.path)
            log(f'存下一段 {self.path.parent.name}/{self.path.name}：{duration / 60:.1f} 分钟，人声 {speech:.0f} 秒')
        else:
            self.part.unlink(missing_ok=True)
            if keep: log(f'编码失败，丢掉 {self.path.name}（ffmpeg 退出码 {self.proc.returncode}）')


class Segmenter:
    """逐帧喂 (pcm, 是否人声, 这一帧的钟点)，按开头说的规则开段、切段。open_writer(start) 返回一个有 write/close 的对象。"""

    def __init__(self, cfg: Config, open_writer):
        self.cfg, self.open_writer = cfg, open_writer
        self.pre = collections.deque(maxlen=int(PREROLL / FRAME_S))
        self.recent = collections.deque(maxlen=int(START_WINDOW / FRAME_S))
        self.writer = None
        self.seg_start = self.last_speech = None
        self.frames = self.speech_frames = 0

    @property
    def recording(self) -> bool: return self.writer is not None

    def feed(self, pcm: bytes, voiced: bool, t: datetime) -> None:
        self.recent.append(voiced)
        if self.writer is None:
            self.pre.append((pcm, voiced, t))
            if sum(self.recent) * FRAME_S >= START_SPEECH:
                self._open()
            return
        self._write(pcm, voiced)
        n = int(ACTIVE_WINDOW / FRAME_S)
        if sum(list(self.recent)[-n:]) >= n * ACTIVE_RATIO:
            self.last_speech = t
        if (t - self.last_speech).total_seconds() >= self.cfg.silence_end:
            self.close()
        elif self.frames * FRAME_S >= self.cfg.max_segment:
            self.close()
            self._open(continuing=t + timedelta(seconds=FRAME_S))   # 这一帧已经写进上一段

    def _open(self, continuing: datetime | None = None) -> None:
        frames = [] if continuing else list(self.pre)
        self.seg_start = continuing or frames[0][2]
        self.last_speech = continuing or frames[-1][2]
        self.writer = self.open_writer(self.seg_start)
        self.frames = self.speech_frames = 0
        for pcm, voiced, _ in frames:
            self._write(pcm, voiced)
        self.pre.clear()

    def _write(self, pcm: bytes, voiced: bool) -> None:
        self.writer.write(pcm)
        self.frames += 1
        self.speech_frames += voiced

    def close(self) -> None:
        """结束当前这段（没在录就什么也不做）。"""
        if self.writer is None: return
        speech = self.speech_frames * FRAME_S
        self.writer.close(keep=speech >= self.cfg.min_speech, duration=self.frames * FRAME_S, speech=speech)
        self.writer = None
        self.recent.clear()


def _read_exact(f, n: int) -> bytes:
    buf = b''
    while len(buf) < n:
        chunk = f.read(n - len(buf))
        if not chunk: return b''
        buf += chunk
    return buf


def _status(cfg: Config, **kw) -> None:
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    (cfg.data_dir / 'recorder.json').write_text(json.dumps({'updated': datetime.now().isoformat(timespec='seconds'), **kw}, ensure_ascii=False))


def capture(cfg: Config) -> None:
    """录音窗口内的一轮：开 ffmpeg 读麦克风，一直录到出了窗口、ffmpeg 退出或收到 SIGTERM。"""
    import webrtcvad
    vad = webrtcvad.Vad(2)
    seg = Segmenter(cfg, lambda start: OpusWriter(cfg.audio_dir, start))
    ff = subprocess.Popen(['ffmpeg', '-nostdin', '-hide_banner', '-v', 'error', '-f', 'avfoundation', '-i', f':{cfg.device}',
                           '-ac', '1', '-ar', str(RATE), '-f', 's16le', '-'], stdout=subprocess.PIPE)
    # 录音窗口里不让系统闲置睡眠，否则人坐着聊天、不碰电脑，十来分钟后就睡过去了。合盖照样会睡，麦克风也会断。
    awake = subprocess.Popen(['caffeinate', '-i', '-w', str(ff.pid)])
    base, n, level = None, 0, 0
    log(f'开始录音：{cfg.device}')
    try:
        while True:
            pcm = _read_exact(ff.stdout, FRAME_BYTES)
            if not pcm:
                log(f'ffmpeg 不再出音频（退出码 {ff.poll()}）'); break
            if base is None:                              # 麦克风刚打开时头几秒可能没数据，从第一帧到手算起
                base = datetime.now() - timedelta(seconds=FRAME_S)
            t = base + timedelta(seconds=n * FRAME_S)
            n += 1
            seg.feed(pcm, vad.is_speech(pcm, RATE), t)
            if n % 100: continue                          # 每 3 秒看一次时钟
            now = datetime.now()
            if (now - t).total_seconds() > GAP:
                log(f'音频断了约 {(now - t).total_seconds():.0f} 秒（多半是睡眠），这段到此为止')
                seg.close()
                base, n = now, 0
            if not cfg.in_window(now):
                log('出了录音时段'); break
            if n % 2000 == 0 or seg.recording != level:   # 状态变了马上写，平时一分钟写一次
                level = seg.recording
                _status(cfg, state='recording' if seg.recording else 'listening',
                        segment=seg.seg_start.isoformat(timespec='seconds') if seg.recording else None)
    finally:
        seg.close()
        ff.terminate()
        try: ff.wait(timeout=10)
        except subprocess.TimeoutExpired: ff.kill()
        awake.terminate()


def run(cfg: Config) -> None:
    """常驻（launchd 拉起）：录音时段里录，时段外每 30 秒看一次时间。ffmpeg 出错就等 30 秒再开。"""
    def stop(*_): raise SystemExit(0)
    signal.signal(signal.SIGTERM, stop)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    while True:
        if not cfg.in_window(datetime.now()):
            _status(cfg, state='off_hours')
            _time.sleep(30); continue
        _status(cfg, state='listening')
        capture(cfg)
        _time.sleep(30)


if __name__ == '__main__':
    from .config import load
    run(load())
    sys.exit(0)
