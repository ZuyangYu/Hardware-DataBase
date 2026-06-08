# src/core/logger.py
import logging
import os
from datetime import datetime
import config.settings

os.makedirs(config.settings.LOG_DIR, exist_ok=True)

LOG_FILE = os.path.join(config.settings.LOG_DIR, f"rag_{datetime.now().strftime('%Y-%m-%d')}.log")

logger = logging.getLogger("RAG")
logger.setLevel(logging.INFO)
formatter = logging.Formatter("%(asctime)s | %(levelname)-8s | %(message)s")

# 清除已有的处理器
logger.handlers.clear()

# 控制台处理器
ch = logging.StreamHandler()
ch.setFormatter(formatter)
logger.addHandler(ch)

# 文件处理器
fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
fh.setFormatter(formatter)
logger.addHandler(fh)

# 导出函数
log = logger.info
warn = logger.warning
error = logger.error
debug = logger.debug