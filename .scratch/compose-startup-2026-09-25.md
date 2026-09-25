# Compose 启动故障复现记录

## 报告的问题

在仓库根目录执行 `docker compose up --build` 时，用户报告前后端及依赖服务无法启动并出现终端报错。

## 复现环境与前置状态

- 日期：2026-09-25
- 工作目录：`E:\MuseFlow`
- 宿主机：Windows amd64，Docker context `desktop-linux`
- Docker Desktop：4.91.0；Engine/CLI：29.8.0；Docker Compose：v5.5.1
- 启动前 `docker compose ps -a` 显示现有 MuseFlow 容器均已停止。
- Docker daemon 可访问；`docker compose config --quiet` 退出码为 0。
- Compose 使用的 55432、6379、8000、5173、9000、9001 端口当时没有监听进程。

## 固定复现路径与结果

从仓库根目录执行原命令 `docker compose up --build`。所有镜像均构建完成；PostgreSQL、Redis、MinIO、API、Web、scheduler、worker 和 maintenance-worker 均进入 healthy 状态。`migrate` 完成到迁移版本 `0007_reference_operation_leases`，`minio-init` 以代码 0 退出。

运行中检查结果：

- `http://127.0.0.1:8000/api/v1/health/ready`：HTTP 200，返回 `{"status":"ready"}`。
- `http://127.0.0.1:5173/`：HTTP 200，返回前端页面。
- `http://127.0.0.1:5173/api/v1/health/ready`：HTTP 200，前端代理可访问 API。

完整 Compose 输出保存在同目录的 `compose-up-baseline-exact-2026-09-25.log`。日志中未发现构建失败、异常堆栈或 unhealthy；仅发现 Celery worker 以 root 运行的 `SecurityWarning`，它没有阻止服务启动。

一个辅助诊断调用曾额外传入当前 Compose 不支持的 `--progress` 参数，其错误记录保存在 `compose-up-baseline-2026-09-25.log`。该错误由诊断参数引起，不属于用户报告的原命令故障。

## 收尾与结论

验证后执行 `docker compose stop`，所有容器恢复为停止状态；没有删除容器或数据卷。当前环境未复现用户报告的问题，因此尚无证据定位业务代码根因，也没有修改业务代码。需要取得用户终端中的完整原始报错、执行命令时的工作目录和 Docker/Compose 版本，才能固定失败路径并判断是否为端口、镜像拉取/构建、资源或其他环境边界问题。
