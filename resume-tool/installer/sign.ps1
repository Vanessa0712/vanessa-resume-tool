<#
.SYNOPSIS
    给 Resume Tool 的 exe 与安装包做 Authenticode 代码签名。

.DESCRIPTION
    证书来源按以下顺序取第一个可用的：
      1. -PfxPath + -PfxPassword   正式证书最常见形态（.pfx 文件）
      2. -Thumbprint               证书已导入 CurrentUser\My
      3. -SelfSigned               生成/复用开发者自签名证书（仅本机或内网可信）

    签名顺序有严格要求，否则安装包里仍是未签名的 exe：
      先签 dist\ResumeTool\ResumeTool.exe → 再编译安装包 → 最后签安装包
    加 -RebuildInstaller 会自动按这个顺序重编译并签完两者。

.EXAMPLE
    # 开发者自签名（无需购买证书）
    .\installer\sign.ps1 -SelfSigned -RebuildInstaller

.EXAMPLE
    # 拿到正式 pfx 后
    .\installer\sign.ps1 -PfxPath D:\cert\mycert.pfx -PfxPassword '******' -RebuildInstaller

.EXAMPLE
    # 证书已装在证书库
    .\installer\sign.ps1 -Thumbprint 1A2B3C... -RebuildInstaller
#>
param(
    [string]$PfxPath,
    [string]$PfxPassword,
    [string]$Thumbprint,
    [switch]$SelfSigned,
    [switch]$RebuildInstaller,
    [string]$TimestampUrl = 'http://timestamp.digicert.com',
    [string]$IsccPath = "$env:LOCALAPPDATA\Programs\Inno Setup 7\ISCC.exe",
    [string]$RepoRoot = (Split-Path -Parent $PSScriptRoot)
)

$ErrorActionPreference = 'Stop'
$signTool = 'Set-AuthenticodeSignature'

function Write-Step([string]$msg) { Write-Host "==> $msg" }
function Write-Warn([string]$msg) { Write-Host "!!  $msg" }

function Resolve-SigningCert {
    if ($PfxPath) {
        if (-not (Test-Path $PfxPath)) { throw "找不到 pfx 文件：$PfxPath" }
        $secure = ConvertTo-SecureString -String $PfxPassword -AsPlainText -Force
        return [System.Security.Cryptography.X509Certificates.X509Certificate2]::new($PfxPath, $secure)
    }
    if ($Thumbprint) {
        $cert = Get-ChildItem Cert:\CurrentUser\My | Where-Object { $_.Thumbprint -eq $Thumbprint }
        if (-not $cert) { throw "证书库中找不到指纹 $Thumbprint" }
        return $cert
    }
    if ($SelfSigned) {
        $subject = 'CN=Resume Tool, O=Resume Tool, C=CN'
        $cert = Get-ChildItem Cert:\CurrentUser\My -CodeSigningCert -ErrorAction SilentlyContinue |
                Where-Object { $_.Subject -eq $subject -and $_.NotAfter -gt (Get-Date).AddDays(30) } |
                Sort-Object NotAfter -Descending | Select-Object -First 1
        if ($cert) {
            Write-Host "    复用已有自签名证书，到期 $($cert.NotAfter.ToString('yyyy-MM-dd'))"
            return $cert
        }
        Write-Host "    未找到有效自签名证书，新建一张（有效期 3 年）"
        return New-SelfSignedCertificate `
            -Type CodeSigningCert `
            -Subject $subject `
            -FriendlyName 'Resume Tool (self-signed)' `
            -KeyUsage DigitalSignature `
            -CertStoreLocation Cert:\CurrentUser\My `
            -NotAfter (Get-Date).AddYears(3)
    }
    throw "未指定证书：请用 -PfxPath、-Thumbprint 或 -SelfSigned 之一"
}

function Invoke-SignFile {
    param([string]$File, $Cert)

    if (-not (Test-Path $File)) { Write-Warn "跳过（不存在）：$File"; return }
    $item = Get-Item $File
    if ($item.IsReadOnly) { $item.IsReadOnly = $false }

    Write-Host ("    签名 {0}（{1:N1} MB）" -f $item.Name, ($item.Length / 1MB))
    try {
        $sig = Set-AuthenticodeSignature -FilePath $File -Certificate $Cert `
                                         -TimestampServer $TimestampUrl -HashAlgorithm SHA256
    } catch {
        Write-Warn "带时间戳签名失败，改为不带时间戳重试：$($_.Exception.Message)"
        $sig = Set-AuthenticodeSignature -FilePath $File -Certificate $Cert -HashAlgorithm SHA256
    }

    $status = $sig.Status.ToString()
    if ($status -eq 'Valid') {
        Write-Host "    签名有效（已被本机信任）"
    } elseif ($status -eq 'NotTrusted' -or $status -eq 'UnknownError') {
        # 自签名证书不在受信任根里，签名本身成功，只是本机还没信任它
        Write-Warn "签名已写入，但证书未受信任（$status）：目标机需导入证书后才能显示已签名的发布者"
    } else {
        Write-Warn "签名结果：$status — $($sig.StatusMessage)"
    }
    return $sig
}

# ---------- 主流程 ----------
Write-Step "解析签名证书"
$cert = Resolve-SigningCert
Write-Host "    证书：$($cert.Subject)"
Write-Host "    指纹：$($cert.Thumbprint)"
Write-Host "    到期：$($cert.NotAfter.ToString('yyyy-MM-dd'))"

$exePath   = Join-Path $RepoRoot 'dist\ResumeTool\ResumeTool.exe'
$setupGlob = Join-Path $RepoRoot 'dist\installer\*.exe'

Write-Step "签名主程序"
Invoke-SignFile -File $exePath -Cert $cert | Out-Null

if ($RebuildInstaller) {
    Write-Step "重编译安装包（让安装包内嵌入已签名的 exe）"
    if (-not (Test-Path $IsccPath)) { throw "找不到 ISCC.exe：$IsccPath（用 -IsccPath 指定）" }
    $iss = Join-Path $RepoRoot 'installer\ResumeTool.iss'
    & $IsccPath $iss | Select-Object -Last 3
    if ($LASTEXITCODE -ne 0) { throw "ISCC 编译失败，退出码 $LASTEXITCODE" }
}

Write-Step "签名安装包"
$setups = Get-ChildItem $setupGlob -ErrorAction SilentlyContinue
if (-not $setups) { Write-Warn "dist\installer 下没有安装包，跳过" }
foreach ($s in $setups) { Invoke-SignFile -File $s.FullName -Cert $cert | Out-Null }

# 自签名时导出公钥证书，方便分发给同事导入
if ($SelfSigned) {
    Write-Step "导出自签名公钥证书"
    $cerPath = Join-Path $RepoRoot 'dist\installer\ResumeTool-CodeSigning.cer'
    Export-Certificate -Cert $cert -FilePath $cerPath -Type CERT | Out-Null
    Write-Host "    $cerPath"
    Write-Host "    对方导入方式（管理员 PowerShell）：Import-Certificate -FilePath `"$cerPath`" -CertStoreLocation Cert:\LocalMachine\TrustedPeople"
}

Write-Step "校验结果"
foreach ($f in @($exePath) + ($setups | ForEach-Object { $_.FullName })) {
    if (-not (Test-Path $f)) { continue }
    $v = Get-AuthenticodeSignature $f
    $who = if ($v.SignerCertificate) { $v.SignerCertificate.Subject } else { '（无）' }
    Write-Host ("    {0,-34} {1,-12} {2}" -f (Split-Path -Leaf $f), $v.Status, $who)
}
