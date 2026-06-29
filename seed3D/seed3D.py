import os
import threading
from http.server import SimpleHTTPRequestHandler
from socketserver import TCPServer
import time
import argparse
import base64
import shutil
import subprocess
import urllib.request
import requests
import json
import sys
import uuid
from contextlib import contextmanager
from volcenginesdkarkruntime import Ark


# ---------------------------------------------------------------------------
# 路径与常量
# ---------------------------------------------------------------------------
# 本地测试默认路径，部署到 openclaw / ECS 时通过 CLI 参数覆盖即可
LOCAL_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_INBOUND_DIR = os.path.join(LOCAL_BASE_DIR, "inbound")
DEFAULT_PROCESSED_DIR = os.path.join(DEFAULT_INBOUND_DIR, "processed")
DEFAULT_OUTPUT_DIR = os.path.join(LOCAL_BASE_DIR, "output")
DEFAULT_QUEUE_FILE = os.path.join(DEFAULT_INBOUND_DIR, "pending.jsonl")
DEFAULT_DONE_FILE = os.path.join(DEFAULT_INBOUND_DIR, "done.jsonl")
DEFAULT_FAILED_FILE = os.path.join(DEFAULT_INBOUND_DIR, "failed.jsonl")
QUEUE_LOCK_FILE = os.path.join(DEFAULT_INBOUND_DIR, ".queue.lock")


# ---------------------------------------------------------------------------
# 简易跨进程文件锁，确保串行调度器同时只有一个 worker 在弹任务
# ---------------------------------------------------------------------------
@contextmanager
def file_lock(lock_path: str, timeout: float = 30.0, poll: float = 0.2):
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    deadline = time.time() + timeout
    fd = None
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_RDWR)
            break
        except FileExistsError:
            if time.time() > deadline:
                raise TimeoutError(f"Could not acquire lock {lock_path} within {timeout}s")
            time.sleep(poll)
    try:
        yield
    finally:
        if fd is not None:
            os.close(fd)
        try:
            os.remove(lock_path)
        except FileNotFoundError:
            pass


# ---------------------------------------------------------------------------
# 临时下载服务器（仅在需要把模型回传飞书时启动）
# ---------------------------------------------------------------------------
class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # 保持终端整洁


def start_temp_server(directory, port):
    os.chdir(directory)
    with TCPServer(("", port), QuietHandler) as httpd:
        # 设置 180 秒（3分钟）后自动关闭
        threading.Timer(180, httpd.shutdown).start()
        httpd.serve_forever()


# ---------------------------------------------------------------------------
# 图片解析
# ---------------------------------------------------------------------------
def resolve_local_image_to_base64(image_input: str, inbound_dir: str = DEFAULT_INBOUND_DIR) -> str:
    """
    解析传入的图片来源：
      - http(s) 链接：原样返回，交给火山引擎拉取
      - 本地路径：读取并转为 data URI
      - 'latest'：从 inbound_dir 取最新一张图（兜底用，正式队列模式不会走这里）
    """
    if image_input.startswith("http://") or image_input.startswith("https://"):
        return image_input

    target_path = image_input

    if image_input.lower() == "latest" or not os.path.exists(image_input):
        if not os.path.exists(inbound_dir):
            raise FileNotFoundError(f"Directory not found: {inbound_dir}")

        valid_exts = ('.png', '.jpg', '.jpeg', '.webp', '.bmp')
        list_of_files = [
            os.path.join(inbound_dir, f) for f in os.listdir(inbound_dir)
            if f.lower().endswith(valid_exts) and os.path.isfile(os.path.join(inbound_dir, f))
        ]
        if not list_of_files:
            raise FileNotFoundError(f"No images found in {inbound_dir}")

        target_path = max(list_of_files, key=os.path.getmtime)
        print(f"Auto-selected latest image: {target_path}")

    with open(target_path, "rb") as f:
        img_bytes = f.read()

    base64_data = base64.b64encode(img_bytes).decode('utf-8')
    ext = target_path.split('.')[-1].lower()
    if ext == 'jpg':
        ext = 'jpeg'
    elif ext not in ['jpeg', 'png', 'webp', 'bmp']:
        ext = 'png'
    return f"data:image/{ext};base64,{base64_data}"


# ---------------------------------------------------------------------------
# Seed3D 调用
# ---------------------------------------------------------------------------
def generate_usd(image_url: str, inbound_dir: str = DEFAULT_INBOUND_DIR):
    """
    调用 Seed3D 2.0 从图片生成 USD。
    """
    api_key = os.getenv("ARK_API_KEY")
    if not api_key:
        print("Error: ARK_API_KEY is not set. Please set the environment variable.")
        return None

    client = Ark(api_key=api_key)

    try:
        processed_image_data = resolve_local_image_to_base64(image_url, inbound_dir=inbound_dir)
    except Exception as e:
        print(f"Failed to resolve image path: {e}")
        return None

    print("----- create request -----")
    try:
        create_result = client.content_generation.tasks.create(
            model="doubao-seed3d-2-0-260328",
            content=[
                {
                    "type": "text",
                    "text": " --subdivisionlevel medium --fileformat usd"
                },
                {
                    "type": "image_url",
                    "image_url": {"url": processed_image_data}
                }
            ]
        )
    except Exception as e:
        print(f"Failed to create Seed3D task: {e}")
        return None

    task_id = create_result.id
    print(f"Task created with ID: {task_id}")

    print("----- polling request -----")
    while True:
        try:
            get_result = client.content_generation.tasks.get(task_id=task_id)
            if isinstance(get_result, dict):
                status = get_result.get('status')
            else:
                status = getattr(get_result, 'status', None)

            print(f"Task status: {status}")

            if status == "succeeded":
                return get_result
            elif status == "failed":
                print(f"Task failed: {get_result}")
                return None
            elif status == "cancelled":
                print(f"Task cancelled: {get_result}")
                return None
        except Exception as e:
            print(f"Error checking task status: {e}")

        time.sleep(5)


def download_file(get_result, download_dir=DEFAULT_OUTPUT_DIR, output_path=None):
    """
    从结果对象中提取下载链接并保存到本地。
    """
    url = None
    if hasattr(get_result, 'content') and hasattr(get_result.content, 'file_url'):
        url = get_result.content.file_url
    elif isinstance(get_result, dict) and 'content' in get_result:
        if isinstance(get_result['content'], dict):
            url = get_result['content'].get('file_url')

    if not url:
        print(f"Could not find download URL in the result: {get_result}")
        return None

    if output_path:
        if os.path.isdir(output_path) or output_path.endswith(os.sep):
            os.makedirs(output_path, exist_ok=True)
            timestamp = int(time.time())
            filepath = os.path.join(output_path, f"generated_3d_{timestamp}.zip")
        else:
            os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
            filepath = output_path
    else:
        os.makedirs(download_dir, exist_ok=True)
        timestamp = int(time.time())
        filepath = os.path.join(download_dir, f"generated_3d_{timestamp}.zip")

    print(f"Downloading 3D asset from {url[:60]}... to {filepath}")
    try:
        urllib.request.urlretrieve(url, filepath)
        print(f"Downloaded successfully: {filepath}")
        return filepath
    except Exception as e:
        print(f"Failed to download asset: {e}")
        return None


def send_link_to_feishu(download_url, token, receive_id, receive_id_type="open_id"):
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    content = {"text": f"✅ 模型生成成功！\n🔗 下载链接：{download_url}\n(链接3分钟后失效)"}
    payload = {
        "receive_id": receive_id,
        "msg_type": "text",
        "content": json.dumps(content)
    }
    requests.post(
        "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=" + receive_id_type,
        headers=headers,
        json=payload,
    )


# ---------------------------------------------------------------------------
# 队列管理：pending.jsonl 作为 FIFO 队列
# ---------------------------------------------------------------------------
def _read_jsonl(path: str):
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _write_jsonl(path: str, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def _append_jsonl(path: str, row):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def enqueue_task(image_path: str, queue_file: str = DEFAULT_QUEUE_FILE, **extra) -> dict:
    """
    给飞书侧 / 调试用：把一个待处理的图片加入队列。
    """
    task = {
        "request_id": extra.pop("request_id", uuid.uuid4().hex[:12]),
        "image_path": os.path.abspath(image_path),
        "enqueued_at": time.time(),
    }
    task.update(extra)
    with file_lock(QUEUE_LOCK_FILE):
        _append_jsonl(queue_file, task)
    print(f"Enqueued task {task['request_id']} -> {task['image_path']}")
    return task


def pop_next_task(queue_file: str = DEFAULT_QUEUE_FILE):
    """
    弹出队首任务（FIFO）。返回 None 表示队列为空。
    """
    with file_lock(QUEUE_LOCK_FILE):
        rows = _read_jsonl(queue_file)
        if not rows:
            return None
        head, rest = rows[0], rows[1:]
        _write_jsonl(queue_file, rest)
    return head


# ---------------------------------------------------------------------------
# 单任务处理（核心串行逻辑）
# ---------------------------------------------------------------------------
def process_task(
    task: dict,
    output_dir: str = DEFAULT_OUTPUT_DIR,
    processed_dir: str = DEFAULT_PROCESSED_DIR,
    inbound_dir: str = DEFAULT_INBOUND_DIR,
    move_processed: bool = True,
    run_repair: bool = False,
):
    """
    处理单个任务：调用 Seed3D -> 下载 zip -> 移动源图片 -> （可选）validate+repair -> 写日志。
    返回 (filepath, error_message)。
    """
    request_id = task.get("request_id") or uuid.uuid4().hex[:12]
    image_path = task["image_path"]
    print(f"\n===== Processing request_id={request_id} image={image_path} =====")

    if not (image_path.startswith("http://") or image_path.startswith("https://")):
        if not os.path.exists(image_path):
            err = f"Image not found: {image_path}"
            print(err)
            _append_jsonl(DEFAULT_FAILED_FILE, {**task, "error": err, "finished_at": time.time()})
            return None, err

    get_result = generate_usd(image_path, inbound_dir=inbound_dir)
    if not get_result:
        err = "Seed3D generation failed"
        _append_jsonl(DEFAULT_FAILED_FILE, {**task, "error": err, "finished_at": time.time()})
        return None, err

    # 用 request_id 作为输出文件名，便于追溯
    os.makedirs(output_dir, exist_ok=True)
    output_path = os.path.join(output_dir, f"{request_id}.zip")
    filepath = download_file(get_result, download_dir=output_dir, output_path=output_path)
    if not filepath:
        err = "Download failed"
        _append_jsonl(DEFAULT_FAILED_FILE, {**task, "error": err, "finished_at": time.time()})
        return None, err

    # 处理完成后把源图片挪到 processed/，下次不会再被 latest 命中
    if move_processed and not (image_path.startswith("http://") or image_path.startswith("https://")):
        try:
            os.makedirs(processed_dir, exist_ok=True)
            dest = os.path.join(processed_dir, f"{request_id}_{os.path.basename(image_path)}")
            shutil.move(image_path, dest)
            print(f"Moved source image to {dest}")
        except Exception as e:
            print(f"Warning: failed to move processed image: {e}")

    # 可选：调用 validate_and_repair 跑静态规则校验+修复
    repair_summary = None
    if run_repair:
        repair_summary = _run_validate_and_repair(
            zip_path=filepath,
            request_id=request_id,
        )

    done_record = {
        **task,
        "output_path": filepath,
        "finished_at": time.time(),
    }
    if repair_summary is not None:
        done_record["repair"] = repair_summary
    _append_jsonl(DEFAULT_DONE_FILE, done_record)
    return filepath, None


def _run_validate_and_repair(zip_path: str, request_id: str) -> dict:
    """
    调用同目录下的 validate_and_repair.py 子进程，返回结构化摘要。
    解耦原因：validate_and_repair 依赖 omni-asset-cli / usd-simready-cli，
    放子进程里跑可以隔离它们的副作用与导入失败。
    """
    script = os.path.join(LOCAL_BASE_DIR, "validate_and_repair.py")
    cmd = [
        sys.executable,
        script,
        zip_path,
        "--request-id",
        request_id,
    ]
    print("----- running validate_and_repair -----")
    try:
        completed = subprocess.run(cmd, check=False, text=True, capture_output=True)
    except Exception as e:
        print(f"validate_and_repair invocation failed: {e}")
        return {"status": "failed", "error": str(e)}

    summary: dict = {
        "exit_code": completed.returncode,
        "status": "ok" if completed.returncode == 0 else "failed",
    }
    # validate_and_repair 把 PipelineReport 直接打到 stdout，是 JSON
    try:
        report = json.loads(completed.stdout)
        summary["overall_status"] = report.get("overall_status")
        summary["summary"] = report.get("summary")
        summary["pipeline_report"] = os.path.join(
            LOCAL_BASE_DIR, "workspace", request_id, "pipeline.json"
        )
        summary["output_usdc"] = report.get("output_usdc")
    except Exception as e:
        summary["parse_error"] = str(e)
        summary["stderr"] = (completed.stderr or "").strip()[:500]
    return summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Generate USD 3D asset from Image using Seed3D. "
                    "Supports single-shot mode and a serial queue mode driven by pending.jsonl."
    )
    parser.add_argument("image_url", nargs="?",
                        help="(Single-shot) URL or local path of the source image. "
                             "Ignored when --pop-queue or --enqueue is used.")
    parser.add_argument("--pop-queue", action="store_true",
                        help="Pop the head task from the queue file and process it. "
                             "Use this from an external serial scheduler (e.g. openclaw).")
    parser.add_argument("--enqueue", metavar="IMAGE_PATH",
                        help="Append an image path to the queue file and exit. "
                             "Useful for the Feishu webhook side.")
    parser.add_argument("--queue-file", default=DEFAULT_QUEUE_FILE,
                        help=f"Path to the pending queue jsonl. Default: {DEFAULT_QUEUE_FILE}")
    parser.add_argument("--inbound-dir", default=DEFAULT_INBOUND_DIR,
                        help=f"Inbound directory for raw images. Default: {DEFAULT_INBOUND_DIR}")
    parser.add_argument("--processed-dir", default=DEFAULT_PROCESSED_DIR,
                        help=f"Where consumed images are moved. Default: {DEFAULT_PROCESSED_DIR}")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR,
                        help=f"Where generated zips are saved. Default: {DEFAULT_OUTPUT_DIR}")
    parser.add_argument("--output_path", help="(Single-shot) explicit output zip path.", default=None)
    parser.add_argument("--no-move", action="store_true",
                        help="Do not move source image into processed/ after success.")
    parser.add_argument("--request-id", help="(Single-shot) override request id.", default=None)
    parser.add_argument("--feishu_token", default=None,
                        help="Feishu tenant access token for returning the asset.")
    parser.add_argument("--receive_id", default=None,
                        help="Feishu receive_id (open_id, chat_id, user_id).")
    parser.add_argument("--receive_id_type", default="open_id",
                        help="Type of receive_id (default: open_id).")
    parser.add_argument("--repair", action="store_true",
                        help="Run validate_and_repair.py on the generated zip after download.")

    args = parser.parse_args()

    # ---- Mode 1: enqueue only (Feishu webhook 侧调用) ----
    if args.enqueue:
        extra = {}
        if args.feishu_token:
            extra["feishu_token"] = args.feishu_token
        if args.receive_id:
            extra["receive_id"] = args.receive_id
            extra["receive_id_type"] = args.receive_id_type
        enqueue_task(args.enqueue, queue_file=args.queue_file, **extra)
        return

    # ---- Mode 2: pop one task from queue and process (openclaw 串行调度) ----
    if args.pop_queue:
        task = pop_next_task(queue_file=args.queue_file)
        if not task:
            print("Queue is empty, nothing to do.")
            return
    else:
        # ---- Mode 3: single-shot (向后兼容旧用法) ----
        if not args.image_url:
            parser.error("image_url is required unless --pop-queue or --enqueue is used")
        task = {
            "request_id": args.request_id or uuid.uuid4().hex[:12],
            "image_path": args.image_url,
            "enqueued_at": time.time(),
        }
        if args.feishu_token:
            task["feishu_token"] = args.feishu_token
        if args.receive_id:
            task["receive_id"] = args.receive_id
            task["receive_id_type"] = args.receive_id_type

    filepath, err = process_task(
        task,
        output_dir=args.output_dir,
        processed_dir=args.processed_dir,
        inbound_dir=args.inbound_dir,
        move_processed=not args.no_move,
        run_repair=args.repair,
    )
    if err or not filepath:
        sys.exit(1)

    # 飞书回传：把生成的 zip 通过临时 HTTP server 暴露 3 分钟
    feishu_token = task.get("feishu_token")
    receive_id = task.get("receive_id")
    if feishu_token and receive_id:
        public_host = os.getenv("SEED3D_PUBLIC_HOST", "127.0.0.1:8888")
        public_url = f"http://{public_host}/{os.path.basename(filepath)}"
        send_link_to_feishu(
            public_url,
            feishu_token,
            receive_id,
            task.get("receive_id_type", "open_id"),
        )
        print(f"Temporary server started at {public_url}. Waiting for download...")
        try:
            start_temp_server(os.path.dirname(os.path.abspath(filepath)), 8888)
        except Exception as e:
            print(f"Server error: {e}")


if __name__ == "__main__":
    main()
