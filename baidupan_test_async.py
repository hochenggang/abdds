"""
abdds 集成测试脚本（异步版本）

使用前请设置环境变量:
  BAIDUPAN_CLIENT_ID
  BAIDUPAN_CLIENT_SECRET
  BAIDUPAN_APP_NAME

或直接修改下方配置区域。
"""

import asyncio
import os
import hashlib
import logging
import time
from pathlib import Path

import aiofiles

from abdds import AsyncBaiduPanClient, BaiduPanError


# =================配置区域=================
client_id = os.environ.get("BAIDUPAN_CLIENT_ID", "")
client_secret = os.environ.get("BAIDUPAN_CLIENT_SECRET", "")
app_name = os.environ.get("BAIDUPAN_APP_NAME", "")

# 测试配置
FILE_SIZE_MB = 7
TEST_FILE_SIZE = FILE_SIZE_MB * 1024 * 1024  # 7 MiB
LOCAL_FILE_NAME = "test_source_7mb.bin"
DOWNLOAD_FILE_NAME = "test_downloaded_7mb.bin"
# ===========================================

# 配置日志
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("AsyncIntegrationTest")


async def generate_zero_filled_file(path: Path, size: int):
    """异步创建全 0 测试文件"""
    logger.info(f"正在创建测试文件: {path} ({size} bytes, {size/1024/1024} MiB)...")
    chunk_size = 1024 * 1024  # 1MB
    zeros = b"\x00" * chunk_size
    async with aiofiles.open(path, "wb") as f:
        for _ in range(size // chunk_size):
            await f.write(zeros)
        remaining = size % chunk_size
        if remaining:
            await f.write(b"\x00" * remaining)
    logger.info("文件创建完成。")


async def calculate_file_md5(path: Path) -> str:
    """异步计算本地文件 MD5"""
    logger.info(f"正在计算文件 MD5: {path} ...")
    md5_hash = hashlib.md5()
    async with aiofiles.open(path, "rb") as f:
        while True:
            chunk = await f.read(4096)
            if not chunk:
                break
            md5_hash.update(chunk)
    result = md5_hash.hexdigest()
    logger.info(f"MD5 计算结果: {result}")
    return result


async def run_integration_test():
    if not all([client_id, client_secret, app_name]):
        print("请设置环境变量 BAIDUPAN_CLIENT_ID, BAIDUPAN_CLIENT_SECRET, BAIDUPAN_APP_NAME")
        return

    async with AsyncBaiduPanClient(client_id, client_secret, app_name) as client:
        source_path = Path(LOCAL_FILE_NAME)
        download_path = Path(DOWNLOAD_FILE_NAME)
        remote_path = None

        # 确保登录
        if not client.access_token:
            print(f"\n>>> 请在浏览器打开: {client.auth_url}")
            code = input(">>> 请输入授权码 (Code): ").strip()
            await client.fetch_token(code)

        try:
            # Step 1: 环境准备
            logger.info("=== STEP 1: 环境准备 ===")
            if source_path.exists():
                os.remove(source_path)
            if download_path.exists():
                os.remove(download_path)

            await generate_zero_filled_file(source_path, TEST_FILE_SIZE)
            original_md5 = await calculate_file_md5(source_path)

            # Step 2: 上传文件
            logger.info("\n=== STEP 2: 执行上传 ===")
            start_t = time.time()
            upload_res = await client.upload(source_path, file_to=Path("/test_source_7mb.bin"))
            upload_time = time.time() - start_t

            remote_path = upload_res.path
            logger.info(f"上传成功! 耗时: {upload_time:.2f}s")
            logger.info(f"网盘路径: {remote_path}, fs_id: {upload_res.fs_id}")

            # Step 3: 下载文件
            logger.info("\n=== STEP 3: 执行下载 ===")
            metas = await client.get_file_metas([upload_res.fs_id])
            dlink = metas[0].dlink

            start_t = time.time()
            await client.download(dlink, file_to=download_path)
            download_time = time.time() - start_t
            logger.info(f"下载完成! 耗时: {download_time:.2f}s")

            # Step 4: 核心校验
            logger.info("\n=== STEP 4: 核心校验 ===")
            downloaded_md5 = await calculate_file_md5(download_path)

            logger.info(f"原始文件 MD5: {original_md5}")
            logger.info(f"下载文件 MD5: {downloaded_md5}")

            if original_md5 == downloaded_md5:
                logger.info("测试通过: 数据完整性验证成功！")
            else:
                logger.error("测试失败: 下载的文件与原始文件 MD5 不一致！")
                raise Exception("Data Integrity Check Failed")

        except Exception as e:
            logger.error(f"测试过程中发生异常: {e}")
            raise
        finally:
            # Step 5: 清理环境
            logger.info("\n=== STEP 5: 清理环境 ===")
            if remote_path:
                try:
                    await client.delete_files([remote_path])
                    logger.info(f"已删除远端文件: {remote_path}")
                except Exception as e:
                    logger.warning(f"删除远端文件失败: {e}")

            if source_path.exists():
                os.remove(source_path)
                logger.info(f"已删除本地源文件: {source_path}")

            if download_path.exists():
                os.remove(download_path)
                logger.info(f"已删除下载文件: {download_path}")

            logger.info("测试脚本结束。")


if __name__ == "__main__":
    asyncio.run(run_integration_test())
