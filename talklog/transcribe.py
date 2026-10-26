"""转写：录好的段传到自己的 OSS 存储桶 → 给百炼一个签名链接，qwen-audio 长音频版开说话人分离 → 原始结果存
raw/<日期>/<时分秒>.json → 删掉 OSS 上的文件。桶上另有生命周期规则，talklog/ 下超过 1 天的自动删，兜住中途退出没删成的。

process() 是一轮：把还没转的段都转了，重写涉及到的那几天的 Markdown，再删掉超过保留天数的音频。
launchd 每 5 分钟跑一次；同一时间只跑一轮（文件锁）。

整体用不了（欠费、key 或 OSS 出错、连不上）这一轮就停，下一轮再试；单段失败记一次，满 5 次不再试，raw 里留一个 failed 记录。"""
import fcntl, json, shutil, time, uuid, warnings
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx

from .config import Config, secrets
from . import render

API = 'https://dashscope.aliyuncs.com/api/v1'
PREFIX = 'talklog/'
NO_SPEECH = {'ASR_RESPONSE_HAVE_NO_WORDS', 'SUCCESS_WITH_NO_VALID_FRAGMENT'}
MAX_TRIES = 5
_http = httpx.Client(timeout=60)


def log(msg: str) -> None:
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


class Unavailable(RuntimeError):
    """百炼或 OSS 整体用不了，这一轮别再试。"""


def _need(kv: dict, *keys: str) -> None:
    miss = [k for k in keys if not kv.get(k)]
    if miss: raise Unavailable(f'~/.config/talk-log/env 里缺 {", ".join(miss)}')


def _bucket():
    kv = secrets()
    _need(kv, 'OSS_ACCESS_KEY_ID', 'OSS_ACCESS_KEY_SECRET', 'OSS_BUCKET', 'OSS_ENDPOINT')
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', SyntaxWarning)       # oss2 的文档字符串里有无效转义，新版 Python 编译时会警告
        import oss2
    region = kv['OSS_ENDPOINT'].split('.')[0].removeprefix('oss-')
    return oss2.Bucket(oss2.AuthV4(kv['OSS_ACCESS_KEY_ID'], kv['OSS_ACCESS_KEY_SECRET']), 'https://' + kv['OSS_ENDPOINT'],
                       kv['OSS_BUCKET'], region=region)


def _call(method: str, url: str, headers: dict | None = None, **kw) -> dict:
    """调百炼：限流和服务端错误退避重试；别的错误（欠费、key 不对、请求写错）直接算整体不可用。"""
    kv = secrets(); _need(kv, 'DASHSCOPE_API_KEY')
    err = ''
    for i in range(5):
        try:
            r = _http.request(method, url, headers={'Authorization': f"Bearer {kv['DASHSCOPE_API_KEY']}",
                                                    'Content-Type': 'application/json', **(headers or {})}, **kw)
        except httpx.TransportError as e:
            err = repr(e)
        else:
            if r.status_code == 200: return r.json()
            err = f'HTTP {r.status_code}: {r.text[:300]}'
            if r.status_code not in (429, 500, 502, 503, 504): raise Unavailable(err)
        time.sleep(min(30, 2 ** i) + 0.5)
    raise Unavailable(err)


def _submit(url: str, model: str) -> str:
    d = _call('POST', f'{API}/services/audio/asr/transcription', headers={'X-DashScope-Async': 'enable'},
              json={'model': model, 'input': {'file_urls': [url]},
                    'parameters': {'channel_id': [0], 'language_hints': ['zh', 'en'], 'diarization_enabled': True}})
    return d['output']['task_id']


def _outcome(tid: str) -> tuple[str, dict | str | None]:
    """查一次任务：('running', None)、('no_speech', None)、('failed', 原因)、('ok', 百炼的原始结果)。"""
    o = _call('GET', f'{API}/tasks/{tid}')['output']
    if o.get('task_status') in ('PENDING', 'RUNNING'): return 'running', None
    sub = (o.get('results') or [{}])[0]
    if (sub.get('code') or o.get('code')) in NO_SPEECH: return 'no_speech', None
    if o.get('task_status') != 'SUCCEEDED' or sub.get('subtask_status') != 'SUCCEEDED':
        return 'failed', f"{sub.get('code') or o.get('code')} {sub.get('message') or o.get('message')}"
    r = _http.get(sub['transcription_url'])
    if r.status_code != 200: return 'failed', f'取转写结果失败：HTTP {r.status_code}'
    return 'ok', r.json()


def transcribe_files(files: list[Path], model: str, on_result, wait: float = 3600) -> None:
    """一批音频文件，每段出了结果就调 on_result(文件, 状态, 结果)，状态同 _outcome。整体用不了抛 Unavailable，
    已经出了结果的那些在这之前已经交给 on_result 了。"""
    bucket = _bucket()
    import oss2
    keys, tasks = {}, {}
    try:
        for f in files:
            key = f"{PREFIX}{datetime.now():%Y%m%d}/{f.stem}-{uuid.uuid4().hex[:8]}{f.suffix}"
            try: bucket.put_object_from_file(key, str(f))
            except oss2.exceptions.OssError as e: raise Unavailable(f'传 OSS 失败：{e.status} {e.code} {e.message}') from e
            except Exception as e: raise Unavailable(f'传 OSS 失败：{e!r}') from e
            keys[f] = key
            tasks[f] = _submit(bucket.sign_url('GET', key, 6 * 3600, slash_safe=True), model)
            time.sleep(0.1)
        pending, t0 = dict(tasks), time.time()
        while pending:
            if time.time() - t0 > wait: raise Unavailable(f'等了 {wait / 60:.0f} 分钟还有 {len(pending)} 段没出结果')
            time.sleep(3)
            for f, tid in list(pending.items()):
                time.sleep(0.1)
                state, got = _outcome(tid)
                if state == 'running': continue
                del pending[f]
                on_result(f, state, got)
    finally:
        for key in keys.values():
            try: bucket.delete_object(key)
            except Exception as e: log(f'删 OSS 上的音频失败（1 天后生命周期规则会删）：{key} {e!r}')


def raw_path(cfg: Config, audio: Path) -> Path:
    return cfg.raw_dir / audio.parent.name / f'{audio.stem}.json'


def pending(cfg: Config) -> list[Path]:
    """录好了（.ogg 和 .json 都在）、还没有转写记录的段，按时间先后。"""
    return sorted(a for a in cfg.audio_dir.glob('*/*.ogg') if a.with_suffix('.json').exists() and not raw_path(cfg, a).exists())


def _save(cfg: Config, audio: Path, state: str, got) -> None:
    meta = json.loads(audio.with_suffix('.json').read_text())
    p = raw_path(cfg, audio)
    p.parent.mkdir(parents=True, exist_ok=True)
    if state == 'failed':
        tries = cfg.data_dir / 'tries.json'
        t = json.loads(tries.read_text()) if tries.exists() else {}
        k = f'{audio.parent.name}/{audio.name}'
        t[k] = t.get(k, 0) + 1
        tries.write_text(json.dumps(t, ensure_ascii=False, indent=1))
        log(f'转写失败（第 {t[k]} 次）{k}：{got}')
        if t[k] < MAX_TRIES: return
    p.write_text(json.dumps({'meta': meta, 'state': state, 'model': cfg.model,
                             'result': got if state == 'ok' else None, 'error': got if state == 'failed' else None},
                            ensure_ascii=False))


def cleanup(cfg: Config, today: date) -> None:
    """删掉超过保留天数的音频（按日期目录）。转写记录不删。"""
    cut = today - timedelta(days=cfg.keep_audio_days)
    for d in cfg.audio_dir.glob('*'):
        try: day = date.fromisoformat(d.name)
        except ValueError: continue
        if day < cut:
            shutil.rmtree(d); log(f'删掉 {d.name} 的音频（只留 {cfg.keep_audio_days} 天）')


def process(cfg: Config) -> None:
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    with open(cfg.data_dir / 'process.lock', 'w') as lock:
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: return                      # 上一轮还没跑完
        todo, days = pending(cfg), set()
        if todo:
            log(f'要转 {len(todo)} 段')
            def done(f: Path, state: str, got) -> None:
                _save(cfg, f, state, got)
                days.add(f.parent.name)
            try:
                transcribe_files(todo, cfg.model, done)
            except Unavailable as e:
                log(f'百炼或 OSS 用不了，这一轮先停：{e}')
        for day in sorted(days):
            p = render.write_day(cfg, day)
            log(f'更新了 {p}')
        cleanup(cfg, date.today())
