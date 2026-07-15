# abdds

Async Baidu Disk SDK - 简单、有效、报错清晰的百度网盘异步 Python SDK。

基于 [百度网盘开放平台 API](https://pan.baidu.com/union/doc/) 封装，核心依赖 `httpx` + `aiofiles`，全面异步化。

## 安装

```bash
pip install abdds
```

要求 Python 3.9+。

## 快速开始

### 1. 获取应用凭证

在 [百度网盘开放平台](https://pan.baidu.com/union/doc/) 创建应用，获取 `client_id`、`client_secret` 和 `app_name`。

### 2. 初始化客户端

```python
from pathlib import Path
from abdds import AsyncBaiduPanClient

client = AsyncBaiduPanClient(
    client_id="your_client_id",
    client_secret="your_client_secret",
    app_name="your_app_name",
)
```

### 3. 授权登录

```python
# 首次使用需要授权
if not client.access_token:
    print(f"请在浏览器打开: {client.auth_url}")
    code = input("请输入授权码: ")
    await client.fetch_token(code)
```

Token 会自动保存到 `~/.baidupan/` 目录，下次启动无需重新授权。

### 4. 使用 API

```python
# 查询空间配额
quota = await client.get_quota()
print(f"已用: {quota.used / 1024**3:.1f} GB / 总计: {quota.total / 1024**3:.1f} GB")

# 上传文件 - 支持 Path / bytes / Generator / AsyncGenerator
result = await client.upload(Path("local_file.txt"))
result = await client.upload(Path("data.bin"), file_to=Path("/backup/data.bin"))
result = await client.upload(b"hello world", file_to=Path("/hello.txt"))

# 获取文件元信息
metas = await client.get_file_metas([result.fs_id])
dlink = metas[0].dlink

# 下载文件 - file_to=None 返回异步流式迭代器，file_to=Path 写入文件
path = await client.download(dlink, file_to=Path("downloaded.txt"))

# 流式下载（适合大文件或自定义处理）
stream = await client.download(dlink)
async for chunk in stream:
    process(chunk)

# 列出文件
items = await client.get_file_list("/apps/your_app_name/")
for item in items:
    print(f"{'[DIR]' if item.is_dir else '     '} {item.filename} ({item.size} bytes)")

# 删除文件
await client.delete_files(["/apps/your_app_name/old_file.txt"])

# 使用完毕后关闭
await client.close()
```

推荐使用异步上下文管理器自动关闭：

```python
async with AsyncBaiduPanClient(client_id, client_secret, app_name) as client:
    quota = await client.get_quota()
    # ...
```

或使用异步工厂方法：

```python
client = await AsyncBaiduPanClient.create(client_id, client_secret, app_name)
quota = await client.get_quota()
await client.close()
```

## API 参考

### AsyncBaiduPanClient

| 方法 | 说明 |
|------|------|
| `await upload(file_from, file_to=None)` | 异步上传文件。`file_from` 支持 `Path` / `bytes` / `Generator[bytes]` / `AsyncGenerator[bytes]`；`file_to` 为远程路径 `Path`，默认使用文件名 |
| `await download(dlink, file_to=None, chunk_size=1048576)` | 异步下载文件。`file_to=None` 返回异步字节迭代器；`file_to=Path` 写入本地文件 |
| `await get_quota()` | 获取空间配额，返回 `ApiQuotaInfo` |
| `await get_file_metas(fsids)` | 获取文件元信息，返回 `list[ApiFileMeta]` |
| `await get_file_list(path)` | 列出目录文件，返回 `list[ApiFileListItem]` |
| `await delete_files(file_paths)` | 批量删除文件 |
| `await fetch_token(code)` | 通过授权码获取 Token |
| `await refresh_token()` | 刷新 Token |
| `await close()` | 关闭客户端 |

### 异常体系

```
BaiduPanError
├── BaiduPanNetworkError    # 网络通信异常
├── BaiduPanAPIError        # API 业务异常 (errno, errmsg, request_id)
└── TokenExpiredError       # Token 过期或无效
```

所有异常均继承自 `BaiduPanError`，方便统一捕获。

### 特性

- **全面异步** — 基于 httpx + aiofiles，原生 async/await
- **自动重试** — 网络请求自动重试（可配置次数）
- **Token 管理** — 自动持久化、自动刷新、文件权限限制
- **分片上传** — 大文件自动分片上传（4MB 分片）
- **流式下载** — 支持断点续传、指数重试
- **类型安全** — 完整类型标注，支持 PEP 561 (`py.typed`)
- **报错清晰** — 异常包含 `errno`、`errmsg`、`request_id`、`url` 等上下文

## 与同步版本 bdds 的差异

| 特性 | bdds (同步) | abdds (异步) |
|------|------------|-------------|
| HTTP 客户端 | requests | httpx |
| 文件 I/O | 内置 open | aiofiles |
| 上下文管理器 | `with` / `__enter__` | `async with` / `__aenter__` |
| 上传数据源 | Path / bytes / Generator | Path / bytes / Generator / AsyncGenerator |
| 下载返回 | Generator[bytes] | AsyncGenerator[bytes] |
| Token 回调 | 同步函数 | 异步函数 |
| 测试 mock | responses | respx |

## 开发

```bash
# 安装开发依赖
pip install -e ".[dev]"

# 运行测试
python -m pytest -v

# 构建
pip install build twine
python -m build
```

## License

MIT
