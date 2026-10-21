<#
.SYNOPSIS
    TouManFen 阶段 2 本地进程管理脚本（Windows）。

.DESCRIPTION
    管理网关与 5 个 Flask worker 的启动、状态查询与停止。
    安全约束：
    - 只停止本脚本启动且命令行归属匹配的进程；
    - 不按镜像名批量杀进程（不使用 taskkill /IM python.exe）；
    - 不调用 /predict_api，不触发 LLM 上游请求；
    - 不打印密钥。

.PARAMETER Action
    start / stop / status / restart，默认 status。

.PARAMETER TimeoutSeconds
    可选：覆盖所有组件的启动等待秒数；0 表示使用 processes.yaml 中的值。
#>
[CmdletBinding()]
param(
    [ValidateSet('start', 'stop', 'status', 'restart')]
    [string]$Action = 'status',
    [int]$TimeoutSeconds = 0
)

$ErrorActionPreference = 'Stop'

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = (Resolve-Path (Join-Path $ScriptDir '..\..')).Path
$ServingDir = Join-Path $ProjectRoot 'serving'
$ConfigDir = Join-Path $ServingDir 'config'
$LogDir = Join-Path $ServingDir 'logs'
$WorkerLogDir = Join-Path $LogDir 'workers'
$RunDir = Join-Path $ServingDir 'run'
$StateFile = Join-Path $RunDir 'stage2-state.json'
$ScriptLog = Join-Path $LogDir 'stage2-script.log'
$ProcessesFile = Join-Path $ConfigDir 'processes.yaml'
$WorkersFile = Join-Path $ConfigDir 'workers.yaml'
$ModelsFile = Join-Path $ConfigDir 'models.yaml'
$ParentRoot = Split-Path -Parent $ProjectRoot
$GatewayVenv = Join-Path $ParentRoot 'venv-gateway'
$Py3109 = 'D:\Miniconda3\envs\py3109\python.exe'
$GatewayPython = $Py3109
$GatewayPackages = Join-Path $GatewayVenv 'Lib\site-packages'

$env:PYTHONDONTWRITEBYTECODE = '1'
$env:PYTHONUNBUFFERED = '1'
$env:PYTHONIOENCODING = 'utf-8'
try {
    [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
    $OutputEncoding = [System.Text.UTF8Encoding]::new($false)
} catch {
    # 无控制台宿主时忽略
}

foreach ($dir in @($LogDir, $WorkerLogDir, $RunDir)) {
    if (-not (Test-Path -LiteralPath $dir)) {
        New-Item -ItemType Directory -Force -Path $dir | Out-Null
    }
}

function Write-Log {
    param([string]$Message)
    $line = '{0} {1}' -f (Get-Date).ToString('o'), $Message
    Add-Content -LiteralPath $ScriptLog -Value $line -Encoding UTF8
    Write-Host $line
}

function Fail {
    param([string]$Message)
    Write-Log "ERROR: $Message"
    throw $Message
}

function Test-File {
    param([string]$Path)
    return (Test-Path -LiteralPath $Path -PathType Leaf)
}

function ConvertTo-Hashtable {
    param($InputObject)
    if ($null -eq $InputObject) { return $null }
    if ($InputObject -is [System.Management.Automation.PSCustomObject]) {
        $table = @{}
        foreach ($property in $InputObject.PSObject.Properties) {
            $table[$property.Name] = ConvertTo-Hashtable $property.Value
        }
        return $table
    }
    if ($InputObject -is [System.Collections.IEnumerable] -and -not ($InputObject -is [string])) {
        $items = @()
        foreach ($item in $InputObject) {
            $items += ,(ConvertTo-Hashtable $item)
        }
        return ,$items
    }
    return $InputObject
}

function Read-JsonFile {
    param([string]$Path)
    if (-not (Test-File $Path)) { return $null }
    $raw = Get-Content -LiteralPath $Path -Raw -Encoding UTF8
    if (-not $raw -or -not $raw.Trim()) { return $null }
    return ConvertTo-Hashtable (ConvertFrom-Json $raw)
}

function Save-State {
    param($State)
    $json = $State | ConvertTo-Json -Depth 8
    $tempFile = $StateFile + '.tmp'
    [System.IO.File]::WriteAllText($tempFile, $json, [System.Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $tempFile -Destination $StateFile -Force
}

function Read-YamlAsHashtable {
    param([string]$Path)
    if (-not (Test-File $Path)) { Fail "配置文件不存在: $Path" }
    $json = & $GatewayPython -c "import json,sys,yaml; print(json.dumps(yaml.safe_load(open(sys.argv[1],encoding='utf-8')), ensure_ascii=False))" $Path
    if ($LASTEXITCODE -ne 0 -or -not $json) { Fail "读取 YAML 失败: $Path" }
    return ConvertTo-Hashtable (ConvertFrom-Json $json)
}

function Get-ListeningPid {
    param([int]$Port)
    $connection = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($connection) { return [int]$connection.OwningProcess }
    return $null
}

function Get-ProcInfo {
    param([int]$ProcessId)
    return Get-CimInstance Win32_Process -Filter "ProcessId=$ProcessId" -ErrorAction SilentlyContinue
}

function Test-Owned {
    param($Entry)
    if ($null -eq $Entry) { return $false }
    if (-not $Entry.ContainsKey('pid') -or -not $Entry['pid']) { return $false }
    $info = Get-ProcInfo ([int]$Entry['pid'])
    if ($null -eq $info) { return $false }
    $marker = [string]$Entry['marker']
    if ($marker -and ($info.CommandLine -notlike "*$marker*")) { return $false }
    return $true
}

function Wait-Http {
    param([string]$Url, [int]$TimeoutSeconds)
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        try {
            $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 2 -ErrorAction Stop
            if ($response.StatusCode -eq 200) { return $true }
        } catch {
            # 启动期连接失败是预期情况，继续轮询
        }
        Start-Sleep -Milliseconds 500
    }
    return $false
}

function Get-EnvDefault {
    param([string]$Name, [string]$Default)
    $value = [Environment]::GetEnvironmentVariable($Name)
    if ([string]::IsNullOrWhiteSpace($value)) { return $Default }
    return $value.Trim()
}

function Assert-HostValue {
    param([string]$Name, [string]$Value)
    if ($Value -notmatch '^[A-Za-z0-9][A-Za-z0-9.-]*$') {
        Fail "$Name 不是合法的主机名或 IPv4 地址: $Value"
    }
}

function Assert-PortValue {
    param([string]$Name, [string]$Value)
    $parsed = 0
    if (-not [int]::TryParse($Value, [ref]$parsed) -or $parsed -lt 1 -or $parsed -gt 65535) {
        Fail "$Name 必须是 1~65535 的整数: $Value"
    }
}

function Get-ProbeHost {
    param([string]$ListenHost)
    if ($ListenHost -eq '0.0.0.0') { return '127.0.0.1' }
    return $ListenHost
}

# 网络配置：默认本地单机；局域网用环境变量覆盖（不写死固定 IP）。
$BindHost = Get-EnvDefault 'TMF_BIND_HOST' '127.0.0.1'
Assert-HostValue 'TMF_BIND_HOST' $BindHost
$GatewayHostOverride = Get-EnvDefault 'TMF_GATEWAY_HOST' ''
if ($GatewayHostOverride) { Assert-HostValue 'TMF_GATEWAY_HOST' $GatewayHostOverride }
$GatewayPortOverride = Get-EnvDefault 'TMF_GATEWAY_PORT' ''
if ($GatewayPortOverride) { Assert-PortValue 'TMF_GATEWAY_PORT' $GatewayPortOverride }
$WorkerProbeHost = Get-ProbeHost $BindHost

function Test-LlmLocalConfig {
    $envPath = Join-Path $ProjectRoot '04-LLM\.env'
    if (-not (Test-File $envPath)) { return $false }
    $line = Get-Content -LiteralPath $envPath -Encoding UTF8 | Where-Object { $_ -match '^DEEPSEEK_API_KEY=' } | Select-Object -First 1
    if (-not $line) { return $false }
    $value = ($line -split '=', 2)[1]
    return ($value.Trim().Length -gt 0)
}

function Validate-Environment {
    if (-not (Test-File $GatewayPython)) { Fail "网关 Python 不存在: $GatewayPython" }
    if (-not (Test-File $Py3109)) { Fail "py3109 Python 不存在: $Py3109" }
    foreach ($path in @($ProcessesFile, $WorkersFile, $ModelsFile)) {
        if (-not (Test-File $path)) { Fail "配置文件不存在: $path" }
    }

    Push-Location $ProjectRoot
    try {
        $validator = Join-Path $ScriptDir 'validate_config.py'
        $output = & $GatewayPython $validator 2>&1
        if ($LASTEXITCODE -ne 0) {
            Fail ('配置校验失败: ' + ($output -join ' '))
        }
        Write-Log ('配置校验通过: ' + ($output -join ' '))
    } finally {
        Pop-Location
    }

    $processConfig = Read-YamlAsHashtable $ProcessesFile
    $workerConfig = Read-YamlAsHashtable $WorkersFile
    if (-not $processConfig.ContainsKey('workers')) { Fail 'processes.yaml 缺少 workers 段' }
    if (-not $workerConfig.ContainsKey('workers')) { Fail 'workers.yaml 缺少 workers 段' }
    if ($processConfig['workers'].Count -ne $workerConfig['workers'].Count) {
        Fail 'processes.yaml 与 workers.yaml 的 worker 数量不一致'
    }
    foreach ($name in $processConfig['workers'].Keys) {
        if (-not $workerConfig['workers'].ContainsKey($name)) {
            Fail "processes.yaml 中的 worker '$name' 未在 workers.yaml 注册"
        }
        $baseUrl = [string]$workerConfig['workers'][$name]['base_url']
        $uri = [Uri]$baseUrl
        $processPort = [int]$processConfig['workers'][$name]['port']
        if ($uri.Port -ne $processPort) {
            Fail "worker '$name' 端口不一致: workers.yaml=$($uri.Port), processes.yaml=$processPort"
        }
    }

    $pretrained = Join-Path $ParentRoot 'PretrainedModel\bert-base-chinese'
    $requirements = @{
        rf = @(
            (Join-Path $ProjectRoot '01-rf\save_model\rf.pkl'),
            (Join-Path $ProjectRoot '01-rf\save_model\tfidf.pkl')
        )
        fasttext = @(
            (Join-Path $ProjectRoot '02-fasttext\save_model\word_auto_train.pkl')
        )
        bert = @(
            (Join-Path $ProjectRoot '03-bert\save_model\bert.pkl'),
            $pretrained
        )
        distill = @(
            (Join-Path $ProjectRoot '05-distill\save_model\student_v2b.pkl'),
            $pretrained
        )
    }
    foreach ($name in $requirements.Keys) {
        foreach ($path in $requirements[$name]) {
            if (-not (Test-Path -LiteralPath $path)) {
                Fail "worker '$name' 依赖缺失: $path"
            }
        }
    }

    if (-not (Test-LlmLocalConfig)) {
        Write-Log 'WARN: 04-LLM/.env 缺少非空 DEEPSEEK_API_KEY；LLM worker 将跳过启动并标记不可用'
        return $false
    }
    return $true
}

function Start-Worker {
    param(
        [string]$Name,
        $State,
        $ProcessConfig,
        [string]$RunId,
        [int]$TimeoutOverride
    )
    $worker = $ProcessConfig['workers'][$Name]
    $moduleDir = Join-Path $ProjectRoot ([string]$worker['module_dir'])
    $script = [string]$worker['script']
    $port = [int]$worker['port']
    $timeout = if ($TimeoutOverride -gt 0) { $TimeoutOverride } else { [int]$worker['startup_timeout_s'] }
    $marker = "--tmf-managed=$RunId"

    $existing = $null
    if ($State['workers'].ContainsKey($Name)) { $existing = $State['workers'][$Name] }
    if ($existing -and (Test-Owned $existing)) {
        Write-Log "worker '$Name' 已在运行（PID $($existing['pid'])）"
        return
    }

    $occupiedBy = Get-ListeningPid $port
    if ($occupiedBy) {
        Fail "worker '$Name' 端口 $port 已被非本脚本进程占用（PID $occupiedBy），拒绝终止或覆盖"
    }

    $outLog = Join-Path $WorkerLogDir "$Name.out.log"
    $errLog = Join-Path $WorkerLogDir "$Name.err.log"
    $process = Start-Process -FilePath $Py3109 -ArgumentList @($script, $marker) -WorkingDirectory $moduleDir -PassThru -WindowStyle Hidden -RedirectStandardOutput $outLog -RedirectStandardError $errLog
    $entry = @{
        pid = [int]$process.Id
        port = $port
        marker = $marker
        module_dir = $moduleDir
        script = $script
        command = "$Py3109 $script $marker"
        started_at = (Get-Date).ToUniversalTime().ToString('o')
        out_log = $outLog
        err_log = $errLog
        status = 'starting'
        exit_code = $null
    }
    $State['workers'][$Name] = $entry
    Save-State $State
    Write-Log "worker '$Name' 启动中（PID $($process.Id)，port $port）"

    $ready = Wait-Http "http://${WorkerProbeHost}:$port/readyz" $timeout
    if ($ready) {
        $entry['status'] = 'ready'
        Write-Log "worker '$Name' 就绪"
    } else {
        $entry['status'] = 'not_ready'
        Write-Log "WARN: worker '$Name' 在 ${timeout}s 内未就绪（详见 $errLog）"
    }
    if ($process.HasExited) { $entry['exit_code'] = [int]$process.ExitCode } else { $entry['exit_code'] = $null }
    Save-State $State
}

function Start-Gateway {
    param($State, $ProcessConfig, [int]$TimeoutOverride)
    $gateway = $ProcessConfig['gateway']
    $port = if ($GatewayPortOverride) { [int]$GatewayPortOverride } else { [int]$gateway['port'] }
    $hostName = if ($GatewayHostOverride) { $GatewayHostOverride } else { [string]$gateway['host'] }
    Assert-PortValue 'gateway port' ([string]$port)
    Assert-HostValue 'gateway host' $hostName
    $probeHost = Get-ProbeHost $hostName
    $timeout = if ($TimeoutOverride -gt 0) { $TimeoutOverride } else { [int]$gateway['startup_timeout_s'] }
    $marker = 'serving.gateway.main:app'

    if ($State['gateway'] -and (Test-Owned $State['gateway'])) {
        Write-Log "gateway 已在运行（PID $($State['gateway']['pid'])）"
        return
    }

    $occupiedBy = Get-ListeningPid $port
    if ($occupiedBy) {
        Fail "网关端口 $port 已被非本脚本进程占用（PID $occupiedBy），拒绝终止或覆盖"
    }

    $outLog = Join-Path $LogDir 'gateway.out.log'
    $errLog = Join-Path $LogDir 'gateway.err.log'
    $arguments = @(
        '-m', 'uvicorn', [string]$gateway['app'],
        '--host', $hostName,
        '--port', [string]$port,
        '--no-access-log',
        '--log-level', 'info'
    )
    $previousPythonPath = $env:PYTHONPATH
    if ([string]::IsNullOrWhiteSpace($previousPythonPath)) {
        $env:PYTHONPATH = $GatewayPackages
    } else {
        $env:PYTHONPATH = "$GatewayPackages;$previousPythonPath"
    }
    try {
        $process = Start-Process -FilePath $GatewayPython -ArgumentList $arguments -WorkingDirectory $ProjectRoot -PassThru -WindowStyle Hidden -RedirectStandardOutput $outLog -RedirectStandardError $errLog
    } finally {
        $env:PYTHONPATH = $previousPythonPath
    }
    $entry = @{
        pid = [int]$process.Id
        port = $port
        marker = $marker
        command = "$GatewayPython -m uvicorn $($gateway['app']) --host $hostName --port $port"
        started_at = (Get-Date).ToUniversalTime().ToString('o')
        out_log = $outLog
        err_log = $errLog
        status = 'starting'
        exit_code = $null
    }
    $State['gateway'] = $entry
    Save-State $State
    Write-Log "gateway 启动中（PID $($process.Id)，port $port）"

    $ready = Wait-Http "http://${probeHost}:$port/api/v1/healthz" $timeout
    if ($ready) {
        $entry['status'] = 'ready'
        Write-Log 'gateway 就绪'
    } else {
        $entry['status'] = 'not_ready'
        Write-Log "WARN: gateway 在 ${timeout}s 内未就绪（详见 $errLog）"
    }
    if ($process.HasExited) { $entry['exit_code'] = [int]$process.ExitCode } else { $entry['exit_code'] = $null }
    Save-State $State
}

function Start-System {
    $llmConfigOk = Validate-Environment
    $processConfig = Read-YamlAsHashtable $ProcessesFile
    $state = Read-JsonFile $StateFile
    if ($null -eq $state) {
        $runId = 'stage2-' + (Get-Date -Format 'yyyyMMddHHmmss') + '-' + ([guid]::NewGuid().ToString('N').Substring(0, 6))
        $state = @{
            run_id = $runId
            started_at = (Get-Date).ToUniversalTime().ToString('o')
            gateway = $null
            workers = @{}
        }
    }
    if (-not $state.ContainsKey('gateway')) { $state['gateway'] = $null }
    if (-not $state.ContainsKey('workers')) { $state['workers'] = @{} }
    $runId = [string]$state['run_id']
    if (-not $runId) {
        $runId = 'stage2-' + (Get-Date -Format 'yyyyMMddHHmmss') + '-' + ([guid]::NewGuid().ToString('N').Substring(0, 6))
        $state['run_id'] = $runId
    }

    foreach ($name in $processConfig['workers'].Keys) {
        if ($name -eq 'llm' -and -not $llmConfigOk) {
            Write-Log '跳过 LLM worker（本地配置缺失），网关会将其标记为不可用'
            continue
        }
        try {
            Start-Worker -Name $name -State $state -ProcessConfig $processConfig -RunId $runId -TimeoutOverride $TimeoutSeconds
        } catch {
            Write-Log "WARN: worker '$name' 启动失败: $_"
        }
    }

    try {
        Start-Gateway -State $state -ProcessConfig $processConfig -TimeoutOverride $TimeoutSeconds
    } catch {
        Write-Log "ERROR: gateway 启动失败: $_"
    }
    Save-State $state

    $failed = @()
    foreach ($name in $processConfig['workers'].Keys) {
        if ($name -eq 'llm' -and -not $llmConfigOk) { continue }
        if (-not $state['workers'].ContainsKey($name)) { $failed += $name; continue }
        if ($state['workers'][$name]['status'] -ne 'ready') { $failed += $name }
    }
    if ($null -eq $state['gateway'] -or $state['gateway']['status'] -ne 'ready') { $failed += 'gateway' }

    if ($failed.Count -gt 0) {
        Write-Log ('启动未全部就绪: ' + ($failed -join ', '))
        exit 1
    }
    Write-Log '阶段 2 本地服务已全部就绪'
}

function Stop-System {
    $state = Read-JsonFile $StateFile
    if ($null -eq $state) {
        Write-Log '未找到状态文件，认为未运行'
        return
    }

    $entries = @()
    if ($state.ContainsKey('gateway') -and $state['gateway']) {
        $entries += @{ name = 'gateway'; entry = $state['gateway'] }
    }
    if ($state.ContainsKey('workers')) {
        foreach ($name in $state['workers'].Keys) {
            $entries += @{ name = $name; entry = $state['workers'][$name] }
        }
    }

    $remaining = $false
    foreach ($item in $entries) {
        $name = [string]$item['name']
        $entry = $item['entry']
        if (-not (Test-Owned $entry)) {
            Write-Log "跳过 $name：进程不存在或归属不匹配（PID $($entry['pid'])）"
            continue
        }
        $processId = [int]$entry['pid']
        $port = [int]$entry['port']
        Write-Log "停止 $name（PID $processId）"
        Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
        Wait-Process -Id $processId -Timeout 10 -ErrorAction SilentlyContinue
        $deadline = (Get-Date).AddSeconds(10)
        while ((Get-Date) -lt $deadline -and (Get-ListeningPid $port)) {
            Start-Sleep -Milliseconds 300
        }
        if (Get-ListeningPid $port) {
            Write-Log "WARN: $name 端口 $port 仍被占用"
            $remaining = $true
        }
    }

    if (-not $remaining) {
        Remove-Item -LiteralPath $StateFile -Force -ErrorAction SilentlyContinue
        Write-Log '全部已停止，状态文件已清理'
    } else {
        Save-State $state
    }
}

function Status-System {
    $state = Read-JsonFile $StateFile
    if ($null -eq $state) {
        Write-Log '未运行（无状态文件）'
        return
    }

    $processConfig = Read-YamlAsHashtable $ProcessesFile
    $gatewayHost = if ($GatewayHostOverride) { $GatewayHostOverride } else { [string]$processConfig['gateway']['host'] }
    $gatewayProbeHost = Get-ProbeHost $gatewayHost
    $gatewayPort = if ($GatewayPortOverride) { [int]$GatewayPortOverride } else { [int]$processConfig['gateway']['port'] }

    $rows = @()
    if ($state.ContainsKey('gateway') -and $state['gateway']) {
        $entry = $state['gateway']
        $owned = Test-Owned $entry
        $port = [int]$entry['port']
        $listening = $null -ne (Get-ListeningPid $port)
        $http = ''
        if ($owned) {
            try {
                $response = Invoke-WebRequest -Uri "http://${gatewayProbeHost}:$port/api/v1/healthz" -UseBasicParsing -TimeoutSec 2 -ErrorAction Stop
                $http = [string]$response.StatusCode
            } catch { $http = 'ERR' }
        }
        $rows += [pscustomobject]@{ Component = 'gateway'; PID = $entry['pid']; Owned = $owned; Port = $port; Listening = $listening; Http = $http; Status = $entry['status'] }
    }
    if ($state.ContainsKey('workers')) {
        foreach ($name in $state['workers'].Keys) {
            $entry = $state['workers'][$name]
            $owned = Test-Owned $entry
            $port = [int]$entry['port']
            $listening = $null -ne (Get-ListeningPid $port)
            $http = ''
            if ($owned) {
                try {
                    $response = Invoke-WebRequest -Uri "http://${WorkerProbeHost}:$port/readyz" -UseBasicParsing -TimeoutSec 2 -ErrorAction Stop
                    $http = [string]$response.StatusCode
                } catch { $http = 'ERR' }
            }
            $rows += [pscustomobject]@{ Component = $name; PID = $entry['pid']; Owned = $owned; Port = $port; Listening = $listening; Http = $http; Status = $entry['status'] }
        }
    }
    $rows | Format-Table -AutoSize | Out-String | Write-Host

    $statePorts = @()
    foreach ($entry in $rows) { $statePorts += [int]$entry.Port }
    $knownPorts = @($statePorts)
    $knownPorts += $gatewayPort
    foreach ($name in $processConfig['workers'].Keys) {
        $knownPorts += [int]$processConfig['workers'][$name]['port']
    }
    foreach ($port in ($knownPorts | Sort-Object -Unique)) {
        $occupiedBy = Get-ListeningPid $port
        if ($occupiedBy -and -not ($statePorts -contains $port)) {
            Write-Log "WARN: 端口 $port 被状态文件之外的进程占用（PID $occupiedBy），本脚本不会处理"
        }
    }
}

try {
    switch ($Action) {
        'start' {
            Start-System
            exit 0
        }
        'stop' {
            Stop-System
            exit 0
        }
        'status' {
            Status-System
            exit 0
        }
        'restart' {
            Stop-System
            Start-Sleep -Seconds 2
            Start-System
            exit 0
        }
    }
} catch {
    Write-Log "FATAL: $_"
    exit 1
}
