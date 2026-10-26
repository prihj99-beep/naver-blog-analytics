$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$desktopPath = [Environment]::GetFolderPath('Desktop')
$shortcutPath = Join-Path $desktopPath '블로그 통계.lnk'
$targetPath = Join-Path $projectRoot 'start_dashboard.bat'
if (-not (Test-Path -LiteralPath $targetPath)) { throw 'start_dashboard.bat was not found.' }
if (Test-Path -LiteralPath $shortcutPath) { throw 'A shortcut named 블로그 통계 already exists. It has not been overwritten.' }
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $targetPath
$shortcut.WorkingDirectory = $projectRoot
$shortcut.Description = '네이버 블로그 검색 통계 로컬 대시보드'
$shortcut.IconLocation = "$env:SystemRoot\System32\shell32.dll,17"
$shortcut.WindowStyle = 7
$shortcut.Save()
Write-Output "Shortcut created: $shortcutPath"
