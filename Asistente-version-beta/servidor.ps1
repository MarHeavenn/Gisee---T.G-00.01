# ---------------------------------------------------------------------------
# Control del servidor de Gisee en Windows (PowerShell)
#
#   .\servidor.ps1 iniciar    arranca el servidor en 0.0.0.0:8000
#   .\servidor.ps1 detener    lo apaga
#   .\servidor.ps1 estado     muestra si esta vivo y las URLs
#   .\servidor.ps1 logs       muestra el registro en vivo (Ctrl+C para salir)
#   .\servidor.ps1 panel      abre el panel de supervision en el navegador
#
# El servidor se ejecuta en segundo plano y escribe:
#   logs\gisee.log            todo (peticiones, errores, avisos)
#   server.log / server.err   salida cruda del proceso
# ---------------------------------------------------------------------------
param(
    [Parameter(Position = 0)]
    [ValidateSet("iniciar", "detener", "estado", "logs", "panel", "reiniciar")]
    [string]$Accion = "estado"
)

$ErrorActionPreference = "Stop"
$Raiz = $PSScriptRoot
$PidFile = Join-Path $Raiz "server.pid"
$LogFile = Join-Path $Raiz "logs\gisee.log"
$UrlPanel = "http://127.0.0.1:8000/panel"

function Obtener-Python {
    $candidato = (Get-Command python -ErrorAction SilentlyContinue)
    if ($candidato) { return $candidato.Source }
    $conocido = "C:\Users\Yopu\AppData\Local\Programs\Python\Python312\python.exe"
    if (Test-Path $conocido) { return $conocido }
    throw "No encuentro python.exe en el PATH."
}

function Escucha-8000 {
    $linea = netstat -ano | Select-String ":8000\s.*LISTENING" | Select-Object -First 1
    if (-not $linea) { return $null }
    $partes = ($linea.ToString().Trim() -split "\s+")
    return [int]$partes[-1]
}

function Servidor-Vivo {
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:8000/health" -TimeoutSec 3 -UseBasicParsing
        return $r.StatusCode -eq 200
    } catch { return $false }
}

$PidGuardado = $null
if (Test-Path $PidFile) {
    $texto = (Get-Content $PidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
    if ($texto -match "^\d+$") { $PidGuardado = [int]$texto }
}

switch ($Accion) {

    "iniciar" {
        if (Servidor-Vivo) {
            Write-Host "El servidor ya esta corriendo." -ForegroundColor Yellow
            Write-Host "Panel: $UrlPanel"
            break
        }
        $ocupado = Escucha-8000
        if ($ocupado) {
            Write-Host "El puerto 8000 esta ocupado por el proceso $ocupado." -ForegroundColor Red
            Write-Host "Apagalo primero (puede ser un 'uvicorn --reload' abierto en otra terminal)."
            break
        }
        $python = Obtener-Python
        New-Item -ItemType Directory -Force -Path (Join-Path $Raiz "logs") | Out-Null
        $proceso = Start-Process -FilePath $python `
            -ArgumentList "main.py" `
            -WorkingDirectory $Raiz `
            -RedirectStandardOutput (Join-Path $Raiz "server.log") `
            -RedirectStandardError  (Join-Path $Raiz "server.err") `
            -PassThru -WindowStyle Hidden
        $proceso.Id | Set-Content $PidFile

        Write-Host "Arrancando el servidor (PID $($proceso.Id))..." -ForegroundColor Cyan
        $listo = $false
        for ($i = 0; $i -lt 60; $i++) {
            Start-Sleep -Seconds 1
            if (Servidor-Vivo) { $listo = $true; break }
        }
        if ($listo) {
            Write-Host "Servidor listo." -ForegroundColor Green
            Write-Host "  Panel:  $UrlPanel"
            Write-Host "  API:    http://127.0.0.1:8000/docs"
            Write-Host "  Salud:  http://127.0.0.1:8000/health"
            Write-Host "  Placa:  http://192.168.20.24:8000"
        } else {
            Write-Host "No respondio en 60 s. Revisa server.err." -ForegroundColor Red
        }
    }

    "detener" {
        if ($PidGuardado -and (Get-Process -Id $PidGuardado -ErrorAction SilentlyContinue)) {
            Stop-Process -Id $PidGuardado -Force
            Write-Host "Servidor detenido (PID $PidGuardado)." -ForegroundColor Green
        } else {
            Write-Host "No hay un servidor administrado corriendo." -ForegroundColor Yellow
        }
        # Cualquier otro proceso que se haya quedado con el puerto (por ejemplo
        # el hijo de un uvicorn --reload) tambien se apaga.
        $siguiente = Escucha-8000
        if ($siguiente) {
            Stop-Process -Id $siguiente -Force -ErrorAction SilentlyContinue
            Write-Host "Libere el puerto 8000 (proceso $siguiente)." -ForegroundColor Yellow
        }
        Remove-Item $PidFile -ErrorAction SilentlyContinue
    }

    "estado" {
        $vivo = Servidor-Vivo
        $ocupado = Escucha-8000
        if ($vivo) {
            Write-Host "SERVIDOR ACTIVO" -ForegroundColor Green
            try {
                $datos = Invoke-RestMethod -Uri "http://127.0.0.1:8000/estado" -TimeoutSec 5
                Write-Host ("  Peticiones: {0}   Errores: {1}   Avisos: {2}" -f `
                    $datos.total_peticiones, $datos.errores, $datos.avisos)
                Write-Host ("  Activo desde: {0} ({1})" -f $datos.inicio, $datos.uptime)
                if ($datos.placa) {
                    Write-Host ("  Placa: {0} - ultima {1} ({2})" -f `
                        $datos.placa.ip, $datos.placa.ruta, $datos.placa.hace)
                } else {
                    Write-Host "  Placa: sin contacto todavia." -ForegroundColor Yellow
                }
            } catch { }
            if ($PidGuardado) { Write-Host "  PID: $PidGuardado" }
            Write-Host "  Panel: $UrlPanel"
        } else {
            Write-Host "SERVIDOR APAGADO" -ForegroundColor Red
            if ($ocupado) { Write-Host "  (el puerto 8000 lo tiene el proceso $ocupado, que no responde a /health)" }
            Write-Host "  Para arrancarlo:  .\servidor.ps1 iniciar"
        }
    }

    "logs" {
        if (-not (Test-Path $LogFile)) {
            Write-Host "Todavia no hay registro. Arranca el servidor primero." -ForegroundColor Yellow
            break
        }
        Write-Host "Registro en vivo ($LogFile) - Ctrl+C para salir" -ForegroundColor Cyan
        Get-Content $LogFile -Tail 40 -Wait
    }

    "panel" {
        if (-not (Servidor-Vivo)) {
            Write-Host "El servidor no responde; arrancalo con:  .\servidor.ps1 iniciar" -ForegroundColor Red
            break
        }
        Start-Process $UrlPanel
        Write-Host "Panel abierto en $UrlPanel"
    }

    "reiniciar" {
        & $PSCommandPath detener
        Start-Sleep -Seconds 2
        & $PSCommandPath iniciar
    }
}
