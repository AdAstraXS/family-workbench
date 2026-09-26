# 精选订阅专用代理

用户于 2026-09-26 选择 NAS 独立代理。使用官方 MetaCubeXD server v1.273.1；
官方镜像摘要 `sha256:433d6cd29e9540a46ee83297faf7c8ff6ef605b606829d61170e26868abaadf8`。
来源：https://github.com/MetaCubeX/metacubexd/blob/main/docs/docker-compose.yml

独立目录 `/volume1/docker/family-workbench-proxy`，目录权限 700。
私有 `.env` 保存两个独立随机值 `CONTROL_TOKEN`、`CLASH_SECRET`，权限 600；不提交，不输出。
订阅配置由用户在面板中填写。不要复用工作台的 `.env`，不要把订阅链接写进仓库、任务脚本或日志。

## 访问范围

- 管理页面和控制 API 仅映射 NAS 127.0.0.1:18780 / 18790，通过批准的 SSH 密钥建立本机隧道访问。
- 7890 代理端口不映射宿主机，只供工作台 Docker 网络使用。
- 无 privileged、host network、NET_ADMIN、TUN、Docker socket 或工作台数据卷。
- `cap_drop: ALL`、`no-new-privileges`、512 MB 内存上限与日志轮转。
- Docker 网络名称已在 DSM 验证为 `family-workbench_default`。

## 工作台接入

只在 DSM 精选订阅命令中注入：

```sh
docker compose exec -T -e PROGRAM_SOURCE_PROXY=http://family-workbench-proxy:7890 web \
  python manage.py run_program_subscriptions --family-id <已核实家庭ID> --max-steps 12
```

这个环境变量不会修改其他模块的网络，也不将百炼 / DeepSeek 密钥交给此代理。
RSS 和正文仅允许固定的 HTTPS 信源域名，并逐次验证重定向；YouTube 下载器只接收已确认频道的视频。
代理端负责域名解析，避免 NAS 本地 DNS 对境外域名返回错误地址。模型 API 和转写结果仍沿用原有公开网络校验。

安装前核对镜像来源和归档哈希；通过 DSM Container Manager 导入镜像、建立独立项目。
现有受限部署包装器不接受任意 Docker 命令，不扩展它来安装代理。
最终验收须从 NAS 工作台实际获取三个境外信源，并在用户填入百炼 Key 后跑完真实转写。
