$ErrorActionPreference = 'Stop'
$Host.UI.RawUI.WindowTitle = 'NAS 分类入口安装：输入 NAS 管理员密码即可'
Write-Host '已准备好固定安装脚本，将校验文件、备份旧入口、安装并验证。' -ForegroundColor Cyan
Write-Host '只需在 [sudo] 密码提示后输入 NAS 管理员 DX 的密码并回车。'
Write-Host '密码输入时不会显示字符，这是正常的。不要把密码发送到聊天。'
& ssh -t -i C:/Users/Administrator/.ssh/family-workbench-nas-ed25519 -o BatchMode=yes -o IdentitiesOnly=yes -o ConnectTimeout=10 DX@192.168.50.86 'sudo /bin/sh /volume1/homes/DX/family-workbench-classification-install-20261006.sh'
$installationResult = $LASTEXITCODE
if ($installationResult -eq 0) {
    Write-Host '安装与验证完成。Codex 会继续独立核验 NAS 状态。' -ForegroundColor Green
} else {
    Write-Host "安装未完成（退出码 $installationResult）。已停止，Codex 会检查原因。" -ForegroundColor Yellow
}
Read-Host '按回车关闭本窗口'
exit $installationResult
