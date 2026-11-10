# setup_python.ps1 - 自动检测 / 下载 / 静默安装 Python（由 start.bat 调用）
# 用法: powershell -NoProfile -ExecutionPolicy Bypass -File setup_python.ps1
#   成功: 向标准输出打印一行 python.exe 完整路径（状态提示走控制台，不进管道）
#   失败: 退出码 1
# 说明: 官方安装包静默参数，仅当前用户（InstallAllUsers=0），不需要管理员权限。

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
} catch { }

# 锁定小版本：与开发验证环境（3.11）同系；3.11.9 是 3.11 系最后一个带官方安装包的版本
$PyVer = '3.11.9'

function Test-RealPython([string]$Exe) {
    if (-not $Exe -or -not (Test-Path -LiteralPath $Exe)) { return $false }
    try {
        & $Exe -c "print(1)" *> $null
        return ($LASTEXITCODE -eq 0)
    } catch { return $false }
}

function Find-Python {
    # 1) PATH 中的 python（排除微软商店的占位假 python）
    try {
        $cmd = Get-Command python.exe -ErrorAction SilentlyContinue
        if ($cmd -and ($cmd.Source -notlike '*WindowsApps*') -and (Test-RealPython $cmd.Source)) { return $cmd.Source }
    } catch { }
    # 2) py 启动器
    try {
        $py = Get-Command py.exe -ErrorAction SilentlyContinue
        if ($py) {
            $out = & $py -3 -c "import sys;print(sys.executable)" 2>$null
            if ($LASTEXITCODE -eq 0 -and $out -and (Test-RealPython ([string]$out[0]))) { return [string]$out[0] }
        }
    } catch { }
    # 3) 常见安装目录（含本脚本之前自动装的）
    try {
        $major = $PyVer.Split('.')[0]; $minor = $PyVer.Split('.')[1]
        $exact = Join-Path $env:LOCALAPPDATA "Programs\Python\Python$major$minor\python.exe"
        if (Test-RealPython $exact) { return $exact }
    } catch { }
    foreach ($root in @(
        (Join-Path $env:LOCALAPPDATA 'Programs\Python'),
        'C:\Program Files\Python',
        'C:\Program Files (x86)\Python'
    )) {
        try {
            $cands = Get-Item -Path (Join-Path $root 'Python3*\python.exe') -ErrorAction SilentlyContinue
            $hit = $cands | Sort-Object {
                $t = $_.Directory.Name -replace '^Python', ''
                if ($t.Length -ge 2) { [version]("{0}.{1}" -f $t.Substring(0,1), $t.Substring(1)) }
                else { [version]'0.0' }
            } -Descending | Select-Object -First 1
            if ($hit -and (Test-RealPython $hit.FullName)) { return $hit.FullName }
        } catch { }
    }
    return $null
}

function Install-Python {
    $arch = 'amd64'
    if ($env:PROCESSOR_ARCHITECTURE -eq 'ARM64') { $arch = 'arm64' }
    elseif (($env:PROCESSOR_ARCHITECTURE -eq 'x86') -and (-not $env:PROCESSOR_ARCHITEW6432)) { $arch = '' }
    $exeName = if ($arch) { "python-$PyVer-$arch.exe" } else { "python-$PyVer.exe" }
    $urls = @(
        "https://registry.npmmirror.com/-/binary/python/$PyVer/$exeName",
        "https://www.python.org/ftp/python/$PyVer/$exeName",
        "https://mirrors.huaweicloud.com/python/$PyVer/$exeName"
    )
    $tmp = Join-Path $env:TEMP $exeName
    $ok = $false
    foreach ($u in $urls) {
        try {
            Write-Host "[i] 没检测到 Python，正在自动下载 Python $PyVer（约 26 MB）..."
            Write-Host "    下载源: $u"
            Invoke-WebRequest -Uri $u -OutFile $tmp -UseBasicParsing -TimeoutSec 600
            if ((Get-Item -LiteralPath $tmp).Length -gt 10MB) { $ok = $true; break }
            Write-Host "[!] 下载不完整，换下一个源..."
        } catch {
            Write-Host "[!] 该源下载失败，自动换下一个源..."
        }
    }
    if (-not $ok) {
        Write-Host "[!] 所有下载源都失败了：请检查网络后，重新双击 start.bat 再试一次。"
        return $null
    }
    Write-Host "[i] 正在安装 Python（静默安装，仅当前用户，不需要管理员，约 1~2 分钟）..."
    $p = Start-Process -FilePath $tmp -ArgumentList '/quiet','/norestart','InstallAllUsers=0','PrependPath=1','Include_test=0','InstallLauncherAllUsers=0' -Wait -PassThru
    if ($p.ExitCode -ne 0 -and $p.ExitCode -ne 3010) {
        Write-Host "[!] Python 安装失败（退出码 $($p.ExitCode)）。"
        Write-Host "    可以手动安装：https://www.python.org/downloads/"
        Write-Host "    安装时务必勾选第一屏下方的 Add python.exe to PATH。"
        return $null
    }
    Remove-Item -LiteralPath $tmp -ErrorAction SilentlyContinue | Out-Null
    Start-Sleep -Seconds 2
    $f = Find-Python
    if ($f) { return $f }
    Write-Host "[!] 安装完成但未能定位 python.exe，请关闭本窗口后重新双击 start.bat 再试。"
    return $null
}

$py = Find-Python
if ($py) {
    Write-Host "[i] 检测到 Python: $py"
    Write-Output $py
    exit 0
}
$py = Install-Python
if ($py) {
    Write-Host "[OK] Python 安装完成: $py"
    Write-Output $py
    exit 0
}
exit 1
