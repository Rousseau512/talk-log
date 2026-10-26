"""talklog 命令：record（常驻录音）、process（转一轮）、try（单个文件试转）、install / uninstall（launchd）、status、today。"""
import argparse, json, os, subprocess, sys, tempfile
from datetime import date, datetime
from pathlib import Path

from . import config, recorder, render, transcribe

AGENTS = Path('~/Library/LaunchAgents').expanduser()
LABELS = {'recorder': 'com.thomas.talklog.recorder', 'process': 'com.thomas.talklog.process'}
PATH = '/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin'


def _plist(label: str, args: list[str], log: Path, extra: str) -> str:
    a = ''.join(f'<string>{x}</string>' for x in args)
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key><array>{a}</array>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>{PATH}</string></dict>
  <key>StandardOutPath</key><string>{log}</string>
  <key>StandardErrorPath</key><string>{log}</string>
  <key>RunAtLoad</key><true/>
{extra}</dict></plist>
'''


def install(_) -> None:
    exe = Path(sys.argv[0]).resolve()
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    AGENTS.mkdir(parents=True, exist_ok=True)
    jobs = {'recorder': ([str(exe), 'record'], '  <key>KeepAlive</key><true/>\n  <key>ProcessType</key><string>Interactive</string>\n'),
            'process': ([str(exe), 'process'], '  <key>StartInterval</key><integer>300</integer>\n')}
    for name, (args, extra) in jobs.items():
        label = LABELS[name]
        p = AGENTS / f'{label}.plist'
        subprocess.run(['launchctl', 'bootout', f'gui/{os.getuid()}/{label}'], capture_output=True)
        p.write_text(_plist(label, args, config.LOG_DIR / f'{name}.log', extra))
        subprocess.run(['launchctl', 'bootstrap', f'gui/{os.getuid()}', str(p)], check=True)
        print(f'已装好 {label}')


def uninstall(_) -> None:
    for label in LABELS.values():
        subprocess.run(['launchctl', 'bootout', f'gui/{os.getuid()}/{label}'], capture_output=True)
        (AGENTS / f'{label}.plist').unlink(missing_ok=True)
        print(f'已卸掉 {label}')


def status(_) -> None:
    cfg = config.load()
    f = cfg.data_dir / 'recorder.json'
    print('录音：', f.read_text() if f.exists() else '还没跑过')
    day = date.today().isoformat()
    segs = sorted((cfg.audio_dir / day).glob('*.ogg'))
    print(f'今天录下 {len(segs)} 段，等着转写的 {len(transcribe.pending(cfg))} 段')
    for name in LABELS:
        log = config.LOG_DIR / f'{name}.log'
        if log.exists():
            tail = log.read_text().strip().splitlines()[-3:]
            print(f'{name}.log 最后几行：', *tail, sep='\n  ')


def today(args) -> None:
    cfg = config.load()
    p = cfg.transcripts_dir / f'{args.day or date.today().isoformat()}.md'
    print(p.read_text() if p.exists() else f'还没有 {p}')


def try_file(args) -> None:
    """单个音频文件试转：转成 16k 单声道 Opus 交百炼（说话人分离只认单声道），打印分好说话人的结果，原始结果存到 --out。"""
    cfg = config.load()
    src = Path(args.file).expanduser()
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / f'{src.stem}.ogg'
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', str(src), '-vn', '-ac', '1', '-ar', '16000', '-c:a', 'libopus',
                        '-b:a', '24k', '-application', 'voip', str(f)], check=True)
        got = {}
        transcribe.transcribe_files([f], cfg.model, lambda _f, state, r: got.update(state=state, result=r), wait=1800)
    if got['state'] != 'ok':
        print(got); return
    if args.out: Path(args.out).write_text(json.dumps(got['result'], ensure_ascii=False, indent=1))
    for t in render.turns(got['result']):
        print(f"{t['start']:7.1f}s {render._who(t['speaker'])}：{t['text']}")
    print('usage:', got['result'].get('usage'))


def main() -> None:
    ap = argparse.ArgumentParser(prog='talklog')
    sub = ap.add_subparsers(required=True)
    sub.add_parser('record', help='常驻录音（launchd 拉起）').set_defaults(fn=lambda _: recorder.run(config.load()))
    sub.add_parser('process', help='转写一轮、更新 Markdown、清旧音频').set_defaults(fn=lambda _: transcribe.process(config.load()))
    p = sub.add_parser('try', help='单个音频文件试转并打印结果'); p.add_argument('file'); p.add_argument('--out'); p.set_defaults(fn=try_file)
    sub.add_parser('install', help='装 launchd 任务并启动').set_defaults(fn=install)
    sub.add_parser('uninstall', help='停掉并删掉 launchd 任务').set_defaults(fn=uninstall)
    sub.add_parser('status', help='看录音和转写状态').set_defaults(fn=status)
    p = sub.add_parser('today', help='打印某天的记录（默认今天）'); p.add_argument('day', nargs='?'); p.set_defaults(fn=today)
    args = ap.parse_args()
    args.fn(args)
