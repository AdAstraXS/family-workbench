# NAS 分类受限入口安装

此文件仅用于已确认的全历史分类批次。应用代码与分类管理页面可通过既有包装器发布；
批次执行前，用户需交互式安装下面的包装器扩展。不要把本地数据库上传到NAS。

新增命令仅调用固定的`preview_asset_classification`，限定家庭ID、日期、确认文件名、
文件SHA-256以及预览摘要。应用强制`--require-complete`，先创建并验证数据库备份。
包装器其余命令保持不变；不修改SSH公钥、sudoers、Compose或生产`.env`。

## 用户一次性安装

2026-10-06用户明确授权代理代为安装并做好备份。可由代理打开
`deploy/open-classification-install.ps1`，用户仅在SSH的sudo提示中输入NAS管理员密码；
`deploy/install-classification-entry.sh`执行固定哈希核对、保留旧入口、原子替换及安装验证。
脚本不接受目标参数，不修改SSH或sudoers，不接收、保存或打印密码。安装后检查失败会
恢复原入口。7项本地临时目录安装测试通过，覆盖成功、幂等、校验失败及回滚。
下列手工步骤仍可供用户自行安装；代理代为执行时无需用户理解或输入这些命令。

准备文件：`deploy/family-workbench-deploy`，已完成7项模拟后端边界测试。
通过既有部署密钥上传至：

```text
/volume1/homes/DX/family-workbench-deploy.classification-bootstrap
```

更新前包装器SHA-256：

```text
27b3d8008df8a8f9236c37e0c59d695d49a54563c4c563d51868383ddb520c03
```

本次新包装器SHA-256：

```text
0b0025d1bf58f603e33e83388c21f2a355146c64c073cfdf84ec691b6bc7ef5c
```

由用户在NAS的SSH终端逐段执行。密码只在交互终端输入，不交给代理，不保存到文件：

```sh
sudo -i
```

以下任一检查失败就停止，不继续覆盖：

```sh
test "$(sha256sum /usr/local/sbin/family-workbench-deploy | awk '{print $1}')" = 27b3d8008df8a8f9236c37e0c59d695d49a54563c4c563d51868383ddb520c03 || exit 1
test "$(sha256sum /volume1/homes/DX/family-workbench-deploy.classification-bootstrap | awk '{print $1}')" = 0b0025d1bf58f603e33e83388c21f2a355146c64c073cfdf84ec691b6bc7ef5c || exit 1
test ! -e /usr/local/sbin/family-workbench-deploy.before-classification-20261006 || exit 1
sh -n /volume1/homes/DX/family-workbench-deploy.classification-bootstrap || exit 1
cp -p /usr/local/sbin/family-workbench-deploy /usr/local/sbin/family-workbench-deploy.before-classification-20261006
install -o root -g root -m 0755 /volume1/homes/DX/family-workbench-deploy.classification-bootstrap /usr/local/sbin/family-workbench-deploy.classification-new
mv /usr/local/sbin/family-workbench-deploy.classification-new /usr/local/sbin/family-workbench-deploy
exit
```

回到DX用户后验证（此步骤只读，不执行分类批次）：

```sh
sudo -n /usr/local/sbin/family-workbench-deploy help
sudo -n /usr/local/sbin/family-workbench-deploy status
```

帮助中应包含`preview-classifications`及`apply-classifications`。安装完成后告知代理，
代理通过同一受限入口完成生产预览、核对及已授权批次。原包装器备份保留供回滚。

## 后续批次

确认JSON为私有家庭资料，仅上传NAS当前家庭部署目录的指定入口；不提交Git。
实际生产日期边界、确认文件及预览摘要经业务服务核对后才能执行。
预览结果有任何待确认项目时暂停，不能用可见字段离线报告摘要替代生产摘要。
执行后核对审计、财务字段摘要、记录数和最新快照日期，再验证重复应用零更新。

包装器安装本身不部署应用或调整历史；代码发布不等于历史批次完成。
