# 家庭投资与记账工作台

这是一个面向群晖 NAS 部署的 Django 项目骨架，用于管理家庭投资组合、收支账本，并预留 AI 分析能力。

## 本地或 NAS 首次启动

1. 复制环境变量文件：

```bash
cp .env.example .env
```

2. 修改 `.env` 中的密码、密钥和群晖 IP。

3. 启动容器：

```bash
docker compose up -d --build
```

容器启动时会先执行 `python manage.py migrate` 来创建或更新数据库表。

如果卡在拉取 `python:3.12-slim`，说明当前网络无法访问 Docker Hub。可以在 `.env` 中把 `PYTHON_IMAGE` 改成你能访问的 Python 镜像，例如公司/家庭代理后的私有镜像，或先在能访问 Docker Hub 的机器上拉取后导入 NAS。

4. 创建管理员账号和第一批示例数据：

```bash
docker compose exec web python manage.py bootstrap_first_data --username admin --password Admin123456
```

这条命令会创建：

- 管理员账号。
- 家庭：我的家庭。
- 成员：我。
- 币种：CNY、HKD、USD。
- 示例证券账户、持仓和交易。
- 示例银行账户、工资收入和餐饮支出。
- AI 服务商占位配置。

5. 访问后台：

```text
http://群晖IP:8000/admin/
```

本地访问通常是：

```text
http://127.0.0.1:8000/admin/
```

首次登录后请立刻在后台修改管理员密码，并把示例账户改成你的真实数据或删除。
