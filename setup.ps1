param([string]$Python = 'python')
$ErrorActionPreference = 'Stop'
$environmentPath = Join-Path $PSScriptRoot '.venv'
& $Python -m venv $environmentPath
if ($LASTEXITCODE -ne 0) { throw 'Python 3.10+ is required to create the MCP environment.' }
$environmentPython = Join-Path $environmentPath 'Scripts/python.exe'
& $environmentPython -m pip install --disable-pip-version-check -r (Join-Path $PSScriptRoot 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'MCP SDK installation failed.' }
Write-Output "Ready. MCP command: $environmentPython"
Write-Output "MCP argument: $(Join-Path $PSScriptRoot 'server.py')"
