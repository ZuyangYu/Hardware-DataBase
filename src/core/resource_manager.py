# src/core/resource_manager.py
import chromadb
import traceback
import threading
import time
import atexit
from typing import Optional
from collections import defaultdict
import config.settings
from src.core.model_factory import init_global_models
from src.core.logger import log, error, warn


class ResourceManager:
    """
    全局资源管理器（线程安全的单例模式 + 上下文管理器）
    """
    _instance: Optional['ResourceManager'] = None
    _init_lock = threading.Lock()

    # 配置参数
    MAX_RETRIES = 3
    RETRY_DELAY_BASE = 2
    CONNECTION_TIMEOUT = 10

    def __new__(cls):
        """双重检查锁定的单例模式"""
        if cls._instance is None:
            with cls._init_lock:
                if cls._instance is None:
                    instance = super().__new__(cls)
                    instance._initialized = False
                    cls._instance = instance
        return cls._instance

    def __init__(self):
        """初始化（只执行一次）"""
        if self._initialized:
            return

        with self._init_lock:
            if self._initialized:
                return

            self._chroma_client: Optional[chromadb.PersistentClient] = None
            self._models_initialized = False
            self._chroma_lock = threading.RLock()
            self._model_lock = threading.RLock()

            # ✅ 使用 RLock (可重入锁) 防止死锁
            self._kb_locks = defaultdict(threading.RLock)
            self._kb_lock_registry_lock = threading.Lock()

            self._health_status = {
                "chroma": False,
                "models": False,
                "last_check": None
            }
            self._is_shutdown = False
            self._initialized = True

            atexit.register(self._atexit_cleanup)
            log("资源管理器已创建")

    def get_kb_lock(self, kb_name: str) -> threading.RLock:
        """获取知识库专属锁"""
        with self._kb_lock_registry_lock:
            return self._kb_locks[kb_name]

    def _atexit_cleanup(self):
        if not self._is_shutdown:
            log("检测到程序退出，执行资源清理...")
            self.shutdown()

    def __enter__(self):
        if not self.initialize():
            raise RuntimeError("资源初始化失败")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.shutdown()
        return False

    def initialize(self, force: bool = False) -> bool:
        if self._is_shutdown:
            warn("资源管理器已关闭，尝试重新初始化...")
            self._is_shutdown = False
            self._chroma_client = None
            self._models_initialized = False
            self._health_status = {
                "chroma": False,
                "models": False,
                "last_check": None
            }

        if not force and self._models_initialized and self._chroma_client is not None:
            log("资源已初始化，跳过")
            return True

        log("=" * 70)
        log("开始初始化全局资源")
        log("=" * 70)

        success = True
        if not self._initialize_models(force):
            success = False
        if not self._initialize_chroma(force):
            success = False

        log("=" * 70)
        if success:
            log("✅ 全局资源初始化完成")
            self._health_status["last_check"] = time.time()
        else:
            error("❌ 全局资源初始化部分失败")
        log("=" * 70)

        return success

    def _initialize_models(self, force: bool = False) -> bool:
        with self._model_lock:
            if self._is_shutdown: return False
            if not force and self._models_initialized: return True

            try:
                log("初始化全局模型...")
                init_global_models()
                self._models_initialized = True
                self._health_status["models"] = True
                return True
            except Exception as e:
                error(f"❌ 模型初始化失败: {e}")
                self._health_status["models"] = False
                traceback.print_exc()
                return False

    def _initialize_chroma(self, force: bool = False) -> bool:
        with self._chroma_lock:
            if self._is_shutdown: return False
            if not force and self._chroma_client is not None:
                if self._test_chroma_connection():
                    return True
                else:
                    self._chroma_client = None

            log(f"连接 ChromaDB: {config.settings.CHROMA_PATH}")
            for attempt in range(1, self.MAX_RETRIES + 1):
                try:
                    self._chroma_client = chromadb.PersistentClient(
                        path=config.settings.CHROMA_PATH,
                        settings=chromadb.Settings(allow_reset=False, anonymized_telemetry=False, is_persistent=True)
                    )
                    if self._test_chroma_connection():
                        self._health_status["chroma"] = True
                        return True
                    else:
                        raise Exception("连接测试失败")
                except Exception as e:
                    error(f"❌ ChromaDB 连接失败 (尝试 {attempt}): {e}")
                    self._chroma_client = None
                    if attempt < self.MAX_RETRIES:
                        time.sleep(self.RETRY_DELAY_BASE ** attempt)
            return False

    def _test_chroma_connection(self) -> bool:
        if self._chroma_client is None: return False
        try:
            self._chroma_client.list_collections()
            return True
        except Exception:
            return False

    @property
    def chroma_client(self) -> chromadb.PersistentClient:
        if self._is_shutdown: raise RuntimeError("资源管理器已关闭")
        with self._chroma_lock:
            if self._chroma_client is None or not self._test_chroma_connection():
                if not self._initialize_chroma(force=True):
                    raise RuntimeError(f"无法连接到 ChromaDB: {config.settings.CHROMA_PATH}")
            return self._chroma_client

    def health_check(self) -> dict:
        if self._is_shutdown: return {"overall": False}
        models_ok = self._models_initialized
        chroma_ok = self._test_chroma_connection()
        return {
            "overall": models_ok and chroma_ok,
            "models": models_ok,
            "chroma": chroma_ok,
            "last_check": time.time()
        }

    def get_status(self) -> dict:
        return {
            "models_initialized": self._models_initialized,
            "chroma_connected": self._chroma_client is not None,
            "is_shutdown": self._is_shutdown,
            "health_status": self._health_status.copy(),
            "chroma_path": config.settings.CHROMA_PATH
        }

    def shutdown(self):
        if self._is_shutdown: return
        log("关闭资源管理器...")
        self._is_shutdown = True
        with self._chroma_lock:
            self._chroma_client = None
        with self._model_lock:
            self._models_initialized = False
        # 清理锁
        with self._kb_lock_registry_lock:
            self._kb_locks.clear()
        log("✅ 资源管理器已完全关闭")


resource_manager = ResourceManager()
