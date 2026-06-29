"""
Standalone Feishu image watcher.

Tails the openclaw runtime log (`/tmp/openclaw/openclaw-YYYY-MM-DD.log`),
correlates inbound messages with downloaded image resources, and triggers
pipeline.run_pipeline() the moment a fresh image lands — completely
independent of the openclaw agent loop. Even if the agent hallucinates,
our pipeline still fires and pushes its own progress messages to Feishu.

Why log-tailing rather than a hook? openclaw-lark exposes no plugin
extension point for inbound media events, but every action we need is
already logged with structured, stable phrasing.

Run:
    /root/3d-venv/bin/python -m simready.feishu_watcher
or:
    /root/3d-venv/bin/python /root/.openclaw/workspace/openclaw_code/simready/feishu_watcher.py

Logs:    stdout (or `--log-file`)
State:   /var/run/feishu_watcher.pos (last byte position) so restart-safe
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import threading
import time
from pathlib import Path

LOCAL_BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(LOCAL_BASE_DIR))

from pipeline import (  # noqa: E402
    FeishuNotifier,
    build_config_card,
    run_pipeline,
)

OPENCLAW_LOG_DIR = Path("/tmp/openclaw")
DEFAULT_STATE_FILE = Path("/tmp/feishu_watcher.pos")
PENDING_DIR = Path("/tmp/simready_pending")
SUBMIT_TIMEOUT_S = 300.0   # 5 分钟未提交 → 按当前 opts 自动开跑

# Log line patterns. The openclaw log emits one JSON object per line with the
# message body in the "1" field; we just grep against the raw line.
RE_RECEIVED = re.compile(
    r"received message from (ou_[a-zA-Z0-9]+) in (oc_[a-zA-Z0-9]+) \((p2p|group)\)"
)
RE_IMAGE_SAVED = re.compile(
    r"feishu: downloaded image resource [^,]+, saved to (\S+\.(?:jpg|jpeg|png|webp|bmp))"
)


def today_log_path():
    """openclaw 进程不一定按本地日期切日志（它会沿用启动那天的文件名直到自然滚动）。
    所以这里挑 /tmp/openclaw/ 下最近被写入的 openclaw-*.log，避免按本地日期猜错文件。"""
    try:
        candidates = list(OPENCLAW_LOG_DIR.glob("openclaw-*.log"))
        if candidates:
            return max(candidates, key=lambda p: p.stat().st_mtime)
    except Exception:
        pass
    today = dt.datetime.now().strftime("%Y-%m-%d")
    return OPENCLAW_LOG_DIR / f"openclaw-{today}.log"


def load_state(state_file):
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
        return data.get("file"), data.get("pos", 0)
    except Exception:
        return None, 0


def save_state(state_file, file, pos):
    try:
        state_file.write_text(json.dumps({"file": file, "pos": pos}), encoding="utf-8")
    except Exception as e:
        print(f"[watcher] failed to save state: {e}", flush=True)


def _write_pending(request_id, payload):
    PENDING_DIR.mkdir(parents=True, exist_ok=True)
    p = PENDING_DIR / f"{request_id}.json"
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _read_pending(request_id):
    p = PENDING_DIR / f"{request_id}.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


# 进程内已处理过的 image_path 集合 — 防止飞书 SDK reconnect 重投递造成重复触发。
_seen_images = set()
# Watcher 启动后的静默窗口：窗口内即使检测到图片也不触发 pipeline，
# 避免 openclaw 在重连时把刚才几分钟内的消息一口气 replay 出来导致风暴。
_startup_ts = time.time()
STARTUP_QUIET_S = 8.0


def trigger_pipeline_async(image_path, open_id, chat_id, chat_type):
    """发配置卡片后立刻启动 pipeline（卡片仅作展示，按默认 opts 跑）。"""
    receive_id = open_id if chat_type == "p2p" else chat_id
    receive_id_type = "open_id" if chat_type == "p2p" else "chat_id"

    def _run():
        request_id = f"fs-{int(time.time())}-{open_id[-6:]}"
        default_opts = {"use_sandbox": True, "use_vlm_physics": True, "repair_3d": True}
        print(f"[watcher] pipeline start: id={request_id} -> {receive_id}", flush=True)

        try:
            notifier = FeishuNotifier(
                receive_id=receive_id,
                receive_id_type=receive_id_type,
                app_id=os.getenv("FEISHU_APP_ID"),
                app_secret=os.getenv("FEISHU_APP_SECRET"),
            )
            notifier.send_card(build_config_card(request_id, default_opts))
            result = run_pipeline(
                image_path=image_path,
                request_id=request_id,
                notifier=notifier,
                options=default_opts,
            )
            print(f"[watcher] pipeline done: id={request_id} status={result.get('status')}", flush=True)
        except Exception as e:
            print(f"[watcher] pipeline crashed: {e}", flush=True)

    threading.Thread(target=_run, daemon=True).start()


def watch(state_file=DEFAULT_STATE_FILE, poll_interval=0.5, dry_run=False, from_start=False):
    """Main tailing loop.

    By default, on first start (no state file) we **seek to end of file**, so
    historical images already in the log are NOT replayed — only images that
    arrive AFTER the watcher starts will trigger the pipeline. Pass
    `from_start=True` for testing if you want to replay everything.

    Correlation strategy: keep the most recent (open_id, chat_id, chat_type)
    seen in a `received message` line — when an `image resource saved to ...`
    line appears within the same handling burst, pair them up.
    """
    state_file.parent.mkdir(parents=True, exist_ok=True)
    last_file, last_pos = load_state(state_file)
    current_file = today_log_path()

    state_file.parent.mkdir(parents=True, exist_ok=True)

    # 同时 tail /tmp/openclaw/ 下所有 openclaw-*.log 文件 —
    # openclaw 实际可能同时往多个日期文件里写。每个文件维护各自的 pos。
    file_positions = {}  # Path -> int

    def _seed_positions():
        """对当前所有日志文件 seek 到末尾（避免回放历史）。"""
        try:
            for p in OPENCLAW_LOG_DIR.glob("openclaw-*.log"):
                if p not in file_positions:
                    try:
                        file_positions[p] = 0 if from_start else p.stat().st_size
                    except Exception:
                        file_positions[p] = 0
        except Exception:
            pass

    _seed_positions()
    print(
        f"[watcher] starting. tailing {len(file_positions)} file(s): "
        f"{[str(p) for p in file_positions]} dry_run={dry_run} from_start={from_start}",
        flush=True,
    )

    pending_sender = None  # (open_id, chat_id, chat_type, ts)
    pending_ttl = 30.0     # seconds — drop stale sender after this

    while True:
        # 周期性扫描新出现的日志文件（跨日新建等）。
        _seed_positions()

        for log_file in list(file_positions.keys()):
            if not log_file.exists():
                continue
            last_pos = file_positions[log_file]
            try:
                with open(log_file, "rb") as f:
                    f.seek(0, os.SEEK_END)
                    size = f.tell()
                    if last_pos > size:
                        last_pos = 0
                    f.seek(last_pos)
                    chunk = f.read()
                    last_pos = f.tell()
            except Exception as e:
                print(f"[watcher] read failed {log_file}: {e}", flush=True)
                continue
            file_positions[log_file] = last_pos

            if not chunk:
                continue
            text = chunk.decode("utf-8", errors="replace")
            for line in text.splitlines():
                m_recv = RE_RECEIVED.search(line)
                if m_recv:
                    pending_sender = (m_recv.group(1), m_recv.group(2), m_recv.group(3), time.time())
                    continue
                m_img = RE_IMAGE_SAVED.search(line)
                if m_img and pending_sender:
                    open_id, chat_id, chat_type, ts = pending_sender
                    if time.time() - ts > pending_ttl:
                        pending_sender = None
                        continue
                    image_path = m_img.group(1)
                    if not os.path.exists(image_path):
                        print(f"[watcher] image not found, skip: {image_path}", flush=True)
                        continue
                    if time.time() - _startup_ts < STARTUP_QUIET_S:
                        print(f"[watcher] startup quiet, skip: {image_path}", flush=True)
                        _seen_images.add(image_path)
                        continue
                    if image_path in _seen_images:
                        print(f"[watcher] already processed, skip: {image_path}", flush=True)
                        continue
                    _seen_images.add(image_path)
                    print(
                        f"[watcher] image detected: from={open_id} chat={chat_id} type={chat_type} "
                        f"path={image_path}",
                        flush=True,
                    )
                    if dry_run:
                        print("[watcher] dry-run: NOT triggering pipeline", flush=True)
                    else:
                        trigger_pipeline_async(image_path, open_id, chat_id, chat_type)
        time.sleep(poll_interval)

        time.sleep(poll_interval)


def main():
    parser = argparse.ArgumentParser(description="Tail openclaw log and trigger Seed3D pipeline on inbound images.")
    parser.add_argument("--state-file", default=str(DEFAULT_STATE_FILE),
                        help="Where the last-read offset is kept. Default: /tmp/feishu_watcher.pos")
    parser.add_argument("--poll-interval", type=float, default=0.5,
                        help="Tail polling interval in seconds.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print detected images but do not call pipeline.")
    parser.add_argument("--from-start", action="store_true",
                        help="Ignore stored offset and start from the top of today's log.")
    args = parser.parse_args()

    state_file = Path(args.state_file)
    if args.from_start and state_file.exists():
        state_file.unlink()

    try:
        watch(
            state_file=state_file,
            poll_interval=args.poll_interval,
            dry_run=args.dry_run,
            from_start=args.from_start,
        )
    except KeyboardInterrupt:
        print("\n[watcher] stopped.", flush=True)


if __name__ == "__main__":
    main()
