# Wan 2.6 单参考图 Provider 探针

此目录只用于阶段 0。探针不接入 MuseFlow 任务、数据库、队列、MinIO 或正式 Provider Adapter。

## 安全默认值

- 默认不发请求；`--check-only` 只生成并检查进程内固定 RGB PNG，不需要凭证。
- 真实创建必须同时提供 `--authorize-real-request`、`--confirm-max-cost-cny 0.20`，且环境中存在 `DASHSCOPE_API_KEY` 和北京地域专属 `DASHSCOPE_API_HOST`。
- 探针固定一次 `POST`、`n=1`、`wan2.6-image`、`enable_interleave=false`。没有创建自动重试；创建超时按“远端可能已受理”处理并立即停止。
- 成功返回 `task_id` 后写入仅本机的 `private-task-state.json`，可用 `--resume` 在同一远端任务上继续轮询。该文件含远端 task ID 和创建时间，不含密钥、账号、URL 或完整响应；超过 23 小时自动失效（比官方 24 小时任务保留期早 1 小时），成功后删除。不要提交它。
- 生成结果只保存在内存中；证据仅记录状态、耗时、图片元数据、摘要和脱敏 ID。结果 URL、输入图片字节和 Provider 完整响应均不写入文件或日志。
- 输入白名单冻结为 PNG/JPEG/WebP、RGB、单帧、无 alpha，原图最多 6,000,000 bytes；本地 Base64 JSON guard 为 8,100,000 bytes。探针实际成功请求的 body 是 978,936 bytes，Provider 更大 body 的接受上限未验证。
- 下载只允许实测命中的单个精确主机，HTTPS，禁止重定向；校验公网 DNS/IP、状态、Content-Type、长度和 Pillow 完整解码。正式 SafeArtifactFetcher 仍须将 DNS/IP 验证绑定到实际连接。

## 检查和测试

在仓库根目录执行：

```powershell
uv run --directory backend --with Pillow pytest E:/MuseFlow/.scratch/provider-feasibility/test_wan26_i2i_probe.py
uv run --directory backend --with Pillow python E:/MuseFlow/.scratch/provider-feasibility/wan26_i2i_probe.py --check-only
```

真实调用须在获得本轮单次对话授权后执行。探针输出完整费用/端点预检，不会因预检通过而自动调用；必须再次附加两个确认参数。成功或失败证据输出到指定 JSON 路径。默认目标路径已存在时会拒绝覆盖。

```powershell
uv run --directory backend --with Pillow python E:/MuseFlow/.scratch/provider-feasibility/wan26_i2i_probe.py --authorize-real-request --confirm-max-cost-cny 0.20 --evidence-output E:/MuseFlow/.scratch/provider-feasibility/evidence/i2i-probe.json
```

本次阶段 0 已经获得单次授权并成功运行一次；脱敏记录位于 `evidence/i2i-probe.json`。后续任何新的收费创建请求均须再次获得单独授权。恢复既有 task 时只查询原任务，不会创建替代任务。
