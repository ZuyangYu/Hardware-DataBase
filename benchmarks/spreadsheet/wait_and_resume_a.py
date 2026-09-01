"""蹲守 Go 套餐 5h 窗口重置, 自动补完 arm a 剩余题目(2 并发省额度)."""
import os
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(REPO)
sys.path.insert(0, REPO)

from openai import OpenAI  # noqa: E402

from src.settings import AGENT_CUSTOM_API_KEY, AGENT_CUSTOM_BASE_URL, AGENT_CUSTOM_MODEL  # noqa: E402

client = OpenAI(base_url=AGENT_CUSTOM_BASE_URL, api_key=AGENT_CUSTOM_API_KEY)
consecutive_ok = 0

while consecutive_ok < 2:
    try:
        client.chat.completions.create(
            model=AGENT_CUSTOM_MODEL, messages=[{"role": "user", "content": "hi"}], max_tokens=5
        )
        consecutive_ok += 1
        print(f"[{time.strftime('%H:%M:%S')}] 可用({consecutive_ok}/2), 再确认一次", flush=True)
        time.sleep(60)
    except Exception as exc:
        consecutive_ok = 0
        tag = "额度未重置" if "UsageLimit" in str(exc) or "429" in str(exc) else str(exc)[:60]
        print(f"[{time.strftime('%H:%M:%S')}] {tag}, 10 分钟后重试", flush=True)
        time.sleep(600)

print(f"[{time.strftime('%H:%M:%S')}] 额度已恢复, 启动 arm a 补跑", flush=True)
subprocess.run(
    [
        ".venv/bin/python", "-u", "benchmarks/spreadsheet/run_answer_eval.py",
        "--arm", "a", "--workers", "2",
    ]
)
