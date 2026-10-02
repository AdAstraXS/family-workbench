# 家庭工作台运行监控

入口：`/monitoring/`，所有已登录家庭成员可查看；管理员维护 `/monitoring/settings/`。

## 数据口径

- 每次模型网络请求分别记录，在解析业务结果之前保存服务商实际返回的 usage。工具循环、分块摘要和重试分别计数。
- 不保存提示词、回答正文、原始错误或密钥。未返回 usage 的调用显示“用量未确认”，不猜测 Token。响应丢失仍可能已扣费。
- 阿里 Fun-ASR 按成功轮询响应的 usage.duration 记录秒数，同一个任务幂等更新；不将语音秒数换成 Token。
- 人民币费用为实际用量乘标准价的估算，每笔保存价格快照。赠送额度、账户折扣、DeepSeek 非高峰折扣未扣除，不是官方账单。
- 同一服务商的同一命名密钥共用余额账户。DeepSeek 复用现有模型密钥；阿里与火山使用独立只读费用中心 AK/SK，加密保存、不回显。智谱余额自动接口暂未接入，显示待配置并链接官网。
- 余额来自官方接口，显示成功时间；失败保留上次值。账户云费用余额可能还承担其他云产品扣费，不代表该模型专属资源包。
- NAS 代理累计流量来自 Clash/Mihomo 全局上传、下载计数的相邻差值，含经过代理的 DIRECT 连接；它不等于运营商 VPN 扣费流量。
- 首次样本只建立基线；检测到计数归零、容器重启或采集缺口时提示不完整，不伪造补算。跨统计日的五分钟采样按采样结束时间归入。
- VPN 剩余额度来自订阅商 subscriptionInfo，包含其他设备。到期日不当成流量重置日。
- 下载文件大小是成功音频下载字节数，不等于网络传输量，不含失败下载及协议开销。
- 备份状态仅确认最近完成的非空 .dump 文件存在；日志更新时间仅表示活动，不代表任务成功。
- 模型及流量自启用后积累，不补算部署前历史用量。

## 自动采集

使用 DSM 用户定义脚本，每天 00:00–23:30、每 30 分钟，root 用户执行以下固定脚本：

```sh
cd /volume1/docker/family-workbench || exit 1
exec 9>logs/runtime-monitor.lock
/usr/bin/flock -n 9 || exit 0
date -Iseconds >> logs/runtime-monitor.log
/usr/bin/python3 /volume1/docker/family-workbench/app/monitoring/nas_probe.py >> logs/runtime-monitor.log 2>&1
```

宿主脚本只读固定代理端点、磁盘、备份文件元数据、指定任务日志时间和容器启动时间。仅将允许的统计字段传给容器内 `collect_monitoring --host-json`；容器不挂载 Docker socket、不获得宿主凭据。采集失败返回非零。

官方余额每小时同步，网页刷新只排队（5 分钟限流）；采集不调用付费模型、不触发下载、转写或投资估值。

## 单价来源（2026-09-28 核对）

- DeepSeek：https://api-docs.deepseek.com/zh-cn/quick_start/pricing/
- 智谱：https://docs.bigmodel.cn/cn/guide/start/pricing
- 方舟：https://docs.volcengine.com/docs/ark/model-pricing?lang=zh
- 北京 Fun-ASR：https://help.aliyun.com/zh/model-studio/fun-asr

管理员可在现有 AI 服务商扩展字段配置 `monitoring_cny_rates`：`input`、`output`、可选 `cached`（人民币/百万 Token）与 `checked` 核对日期。仅影响之后的新请求。

## 验证

新增测试覆盖未知/零用量、缓存扣重、分档价格边界、失败请求、ASR 幂等、余额账户去重、负余额、币种校验、凭据加密及权限、读页面无写入、家庭隔离、CSRF、流量基线/重启/去重、TB 容量与聚合一致性。
离线测试配置不读取生产凭据，不发起真实模型请求。完整回归中的 Windows 文件句柄、富途日志路径、现有日期依赖和 SQLite 并发测试问题应与基线分别核对。
