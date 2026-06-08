# src/core/bm25_cache.py
import os
import pickle
import threading
import glob
from typing import Optional, List
import config.settings
from src.core.logger import log, error


class BM25Cache:
    """
    BM25 索引缓存管理器
    特性:
    - 分库存储: 每个知识库独立存储为 .pkl 文件，避免单点故障
    - 按需加载: 只有在查询特定知识库时才加载其索引
    - 线程安全: 支持并发读写
    """

    _instance: Optional['BM25Cache'] = None
    _init_lock = threading.Lock()

    def __new__(cls):
        """双重检查锁定的单例模式"""
        if cls._instance is None:
            with cls._init_lock:
                if cls._instance is None:
                    instance = super().__new__(cls)
                    cls._instance = instance
                    instance._initialize()
        return cls._instance

    def _initialize(self):
        """初始化"""
        # 创建专属的缓存目录
        self.cache_dir = os.path.join(config.settings.STORAGE_DIR, "bm25_indexes")
        os.makedirs(self.cache_dir, exist_ok=True)

        self.rw_lock = threading.RLock()
        self.mem_cache = {}  # 内存一级缓存: {kb_name: (bm25, ids)}
        log(f"BM25 缓存管理器已就绪，存储目录: {self.cache_dir}")

    def _get_file_path(self, kb_name: str) -> str:
        """获取指定知识库的缓存文件路径"""
        # 简单清洗文件名，防止路径遍历
        safe_name = "".join([c for c in kb_name if c.isalnum() or c in ('_', '-')])
        return os.path.join(self.cache_dir, f"{safe_name}.pkl")

    def get(self, kb_name: str) -> Optional[tuple]:
        """
        获取知识库的 BM25 索引
        Returns: (bm25_obj, id_list) 或 None
        """
        with self.rw_lock:
            # 1. 先查内存缓存
            if kb_name in self.mem_cache:
                return self.mem_cache[kb_name]

            # 2. 内存没有，查磁盘
            file_path = self._get_file_path(kb_name)
            if not os.path.exists(file_path):
                return None

            try:
                # 按需加载
                with open(file_path, "rb") as f:
                    data = pickle.load(f)
                    self.mem_cache[kb_name] = data  # 放入内存
                    log(f"已加载 BM25 索引: {kb_name}")
                    return data
            except Exception as e:
                error(f"❌ 加载 BM25 文件损坏 ({kb_name}): {e}")
                # 文件损坏则移除
                try:
                    os.rename(file_path, file_path + ".corrupt")
                except OSError:
                    pass
                return None

    def set(self, kb_name: str, data: tuple) -> bool:
        """
        保存 BM25 索引到独立文件
        """
        with self.rw_lock:
            try:
                # 1. 更新内存
                self.mem_cache[kb_name] = data

                # 2. 写入磁盘 (原子操作)
                file_path = self._get_file_path(kb_name)
                temp_path = file_path + ".tmp"

                with open(temp_path, "wb") as f:
                    pickle.dump(data, f, protocol=pickle.HIGHEST_PROTOCOL)

                # 替换旧文件
                if os.path.exists(file_path):
                    os.replace(temp_path, file_path)
                else:
                    os.rename(temp_path, file_path)

                log(f"💾 已保存 BM25 索引: {kb_name}")
                return True
            except Exception as e:
                error(f"❌ 保存 BM25 索引失败 ({kb_name}): {e}")
                if kb_name in self.mem_cache:
                    del self.mem_cache[kb_name]
                return False

    def delete(self, kb_name: str) -> bool:
        """删除指定知识库的索引"""
        with self.rw_lock:
            # 清除内存
            if kb_name in self.mem_cache:
                del self.mem_cache[kb_name]

            # 删除文件
            file_path = self._get_file_path(kb_name)
            if os.path.exists(file_path):
                try:
                    os.remove(file_path)
                    log(f"🗑️ 已删除 BM25 索引文件: {kb_name}")
                    return True
                except Exception as e:
                    error(f"删除文件失败: {e}")
                    return False
            return True

    def clear(self) -> bool:
        """清空所有缓存"""
        with self.rw_lock:
            self.mem_cache.clear()
            try:
                files = glob.glob(os.path.join(self.cache_dir, "*.pkl"))
                for f in files:
                    os.remove(f)
                log("✅ 已清空所有 BM25 缓存文件")
                return True
            except Exception as e:
                error(f"清空缓存失败: {e}")
                return False

    def list_all(self) -> List[str]:
        """列出所有有缓存的知识库"""
        files = glob.glob(os.path.join(self.cache_dir, "*.pkl"))
        return [os.path.splitext(os.path.basename(f))[0] for f in files]


# 单例访问
def get_cache() -> BM25Cache:
    return BM25Cache()
