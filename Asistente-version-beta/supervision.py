"""Supervisión de Gisee.

Qué hace este módulo:

* Guarda en memoria las últimas peticiones HTTP (quién, qué ruta, qué código
  de respuesta y cuánto tardó) y las últimas líneas de log.
* Escribe todo a ``logs/gisee.log`` para que también se pueda ver desde una
  terminal con ``Get-Content logs\\gisee.log -Wait``.
* Publica un panel web en ``/panel`` (se refresca solo) y los mismos datos en
  JSON en ``/estado`` por si se quieren consumir desde otro sitio.

La idea es que puedas comprobar de un vistazo que el servidor está vivo, si la
placa se está comunicando, qué pidió y qué falló, sin tener que mirar la
consola de VS Code.
"""

from __future__ import annotations

import io
import logging
import os
import sys
import threading
import time
import traceback
from collections import deque
from datetime import datetime

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE_DIR, "logs")
LOG_FILE = os.path.join(LOG_DIR, "gisee.log")
LOG_FILE_VIEJO = os.path.join(LOG_DIR, "gisee.anterior.log")
MAX_BYTES_LOG = 4 * 1024 * 1024

MAX_PETICIONES = 400
MAX_LOGS = 800

# Rutas que genera el propio panel: no queremos que su sondeo inunde el
# historial de peticiones ni el registro.
RUTAS_INTERNAS = (
    "/panel",
    "/estado",
    "/logs",
    "/favicon.ico",
    "/docs",
    "/openapi.json",
    "/redoc",
)

_inicio = time.time()
_lock = threading.Lock()
_peticiones: deque = deque(maxlen=MAX_PETICIONES)
_logs: deque = deque(maxlen=MAX_LOGS)
_total = 0
_errores = 0          # respuestas 5xx
_avisos = 0           # respuestas 4xx
_placa = None         # última petición recibida desde la placa
_ultimo_tts = None    # última síntesis de voz correcta
_ultimo_chat = None   # última respuesta del chat para la placa

_archivo = None
_archivo_lock = threading.Lock()


# --------------------------------------------------------------------------
# Utilidades
# --------------------------------------------------------------------------

def _hora(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts if ts else time.time()).strftime("%H:%M:%S")


def _rotar_log() -> None:
    """Si el archivo creció demasiado, lo aparta al arrancar."""
    try:
        if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > MAX_BYTES_LOG:
            if os.path.exists(LOG_FILE_VIEJO):
                os.remove(LOG_FILE_VIEJO)
            os.replace(LOG_FILE, LOG_FILE_VIEJO)
    except OSError:
        pass


def _escribir_archivo(linea: str) -> None:
    global _archivo
    try:
        with _archivo_lock:
            if _archivo is None:
                os.makedirs(LOG_DIR, exist_ok=True)
                _archivo = open(LOG_FILE, "a", encoding="utf-8", buffering=1)
            _archivo.write(linea + "\n")
    except OSError:
        # Nunca dejamos que un fallo de disco tumbe el servidor.
        pass


def _guardar_log(nivel: str, origen: str, texto: str, detalle: str = "") -> None:
    ahora = time.time()
    entrada = {
        "ts": ahora,
        "hora": _hora(ahora),
        "nivel": nivel,
        "origen": origen,
        "texto": texto,
        "detalle": detalle,
    }
    with _lock:
        _logs.append(entrada)
    _escribir_archivo(f"{_hora(ahora)} {nivel:<7} {origen}: {texto}")
    if detalle:
        for linea in detalle.rstrip().splitlines():
            _escribir_archivo(f"{'':>17}{linea}")


# --------------------------------------------------------------------------
# Captura de logs y de prints
# --------------------------------------------------------------------------

class _CapturaLog(logging.Handler):
    """Copia al panel todo lo que pasa por el módulo logging (uvicorn incluido)."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            mensaje = record.getMessage()
        except Exception:  # pragma: no cover - defensivo
            mensaje = str(record.msg)

        # El sondeo del panel ensuciaría el registro cada 2 segundos.
        if record.name == "uvicorn.access" and any(r in mensaje for r in RUTAS_INTERNAS):
            return

        detalle = ""
        if record.exc_info:
            detalle = "".join(traceback.format_exception(*record.exc_info))
        _guardar_log(record.levelname, record.name, mensaje, detalle)


class _TeeSalida(io.TextIOBase):
    """Duplica stdout: sigue saliendo a la consola y además queda en el panel."""

    def __init__(self, original):
        self._original = original
        self._parcial = ""

    def writable(self) -> bool:      # para que print no se queje
        return True

    def write(self, texto: str) -> int:
        if self._original is not None:
            try:
                self._original.write(texto)
            except Exception:
                pass
        if not isinstance(texto, str):
            return 0
        self._parcial += texto
        while "\n" in self._parcial:
            linea, self._parcial = self._parcial.split("\n", 1)
            linea = linea.rstrip("\r")
            if not linea.strip():
                continue
            # El sondeo del panel (cada 2 s) no debe inundar el registro.
            if any(r in linea for r in RUTAS_INTERNAS) and "HTTP/1.1" in linea:
                continue
            _guardar_log("SALIDA", "programa", linea)
        return len(texto)

    def flush(self) -> None:
        if self._original is not None:
            try:
                self._original.flush()
            except Exception:
                pass


def instalar_captura() -> None:
    """Engancha la captura de logs. Se llama una sola vez al importar main.py."""
    _rotar_log()

    raiz = logging.getLogger()
    if not any(isinstance(h, _CapturaLog) for h in raiz.handlers):
        raiz.addHandler(_CapturaLog())

    if not isinstance(sys.stdout, _TeeSalida):
        sys.stdout = _TeeSalida(sys.stdout)

    # Marca de arranque legible dentro del propio registro.
    _guardar_log("INFO", "gisee", "Servidor iniciado, panel de supervisión activo")


# --------------------------------------------------------------------------
# Middleware: una línea por petición
# --------------------------------------------------------------------------

def _anotar(request: Request, estado: int, ms: float, interno: bool, detalle: str) -> None:
    global _total, _errores, _avisos, _placa, _ultimo_tts, _ultimo_chat

    ahora = time.time()
    ruta = request.url.path
    cliente = request.client.host if request.client else "?"
    es_local = cliente in ("127.0.0.1", "::1", "localhost")
    origen = "panel" if interno else ("placa" if not es_local else "navegador")

    entrada = {
        "ts": ahora,
        "hora": _hora(ahora),
        "cliente": cliente,
        "origen": origen,
        "metodo": request.method,
        "ruta": ruta,
        "estado": estado,
        "ms": round(ms, 1),
        "detalle": detalle,
    }

    with _lock:
        if not interno:
            _peticiones.append(entrada)
            _total += 1
            if estado >= 500:
                _errores += 1
            elif estado >= 400:
                _avisos += 1
        if not es_local and not interno:
            _placa = {
                "ip": cliente,
                "hora": entrada["hora"],
                "ts": ahora,
                "ruta": ruta,
                "estado": estado,
            }
        if estado < 400 and ruta == "/tts":
            _ultimo_tts = {"hora": entrada["hora"], "ts": ahora}
        if estado < 400 and ruta == "/chat/audio":
            _ultimo_chat = {"hora": entrada["hora"], "ts": ahora}

    if estado >= 400 and not interno:
        _guardar_log(
            "ERROR" if estado >= 500 else "AVISO",
            "http",
            f'{request.method} {ruta} -> {estado} ({origen} {cliente}, {ms:.0f} ms)',
            detalle,
        )


async def registrar_peticion(request: Request, call_next):
    inicio = time.perf_counter()
    interno = request.url.path.startswith(RUTAS_INTERNAS)
    try:
        respuesta = await call_next(request)
    except Exception:
        _anotar(request, 500, (time.perf_counter() - inicio) * 1000, interno,
                traceback.format_exc())
        raise
    _anotar(request, respuesta.status_code, (time.perf_counter() - inicio) * 1000,
            interno, "")
    return respuesta


# --------------------------------------------------------------------------
# Recolección de datos
# --------------------------------------------------------------------------

def _hace(ts: float | None) -> str:
    if not ts:
        return "nunca"
    segundos = max(0, int(time.time() - ts))
    if segundos < 60:
        return f"hace {segundos} s"
    if segundos < 3600:
        return f"hace {segundos // 60} min"
    return f"hace {segundos // 3600} h"


def resumen() -> dict:
    ahora = time.time()
    with _lock:
        peticiones = list(_peticiones)[-80:][::-1]
        logs = list(_logs)[-250:][::-1]
        placa = dict(_placa) if _placa else None
        tts = dict(_ultimo_tts) if _ultimo_tts else None
        chat = dict(_ultimo_chat) if _ultimo_chat else None
        total = _total
        errores = _errores
        avisos = _avisos

    if placa:
        placa["hace"] = _hace(placa.get("ts"))
    if tts:
        tts["hace"] = _hace(tts.get("ts"))
    if chat:
        chat["hace"] = _hace(chat.get("ts"))

    return {
        "activo": True,
        "inicio": datetime.fromtimestamp(_inicio).strftime("%Y-%m-%d %H:%M:%S"),
        "uptime_s": int(ahora - _inicio),
        "uptime": _formatear_uptime(ahora - _inicio),
        "puerto": int(os.getenv("PORT", "8000")),
        "directorio": BASE_DIR,
        "log": LOG_FILE,
        "total_peticiones": total,
        "errores": errores,
        "avisos": avisos,
        "peticiones": peticiones,
        "logs": logs,
        "placa": placa,
        "ultimo_tts": tts,
        "ultimo_chat": chat,
        "chequeos": {
            "groq": bool(os.getenv("GROQ_API_KEY")),
            "escucha": os.path.exists(LOG_FILE),
        },
    }


def _formatear_uptime(segundos: float) -> str:
    segundos = int(segundos)
    dias, resto = divmod(segundos, 86400)
    horas, resto = divmod(resto, 3600)
    minutos, seg = divmod(resto, 60)
    if dias:
        return f"{dias} d {horas} h {minutos} min"
    if horas:
        return f"{horas} h {minutos} min"
    if minutos:
        return f"{minutos} min {seg} s"
    return f"{seg} s"


# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------

router = APIRouter()


@router.get("/estado")
async def estado_json():
    return JSONResponse(resumen())


@router.get("/logs", response_class=PlainTextResponse)
async def logs_texto(limite: int = 200):
    with _lock:
        entradas = list(_logs)[-limite:]
    lineas = []
    for e in entradas:
        lineas.append(f"{e['hora']} {e['nivel']:<7} {e['origen']}: {e['texto']}")
        if e["detalle"]:
            lineas.append(e["detalle"].rstrip())
    return "\n".join(lineas) + "\n"


@router.get("/panel", response_class=HTMLResponse)
async def panel_html():
    return PANEL_HTML


# --------------------------------------------------------------------------
# Panel (HTML autocontenido, sin dependencias externas)
# --------------------------------------------------------------------------

PANEL_HTML = r"""<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Gisee · Panel de supervisión</title>
<style>
  :root{
    --fondo:#0d1117; --caja:#161b22; --borde:#30363d; --texto:#e6edf3;
    --suave:#8b949e; --ok:#3fb950; --mal:#f85149; --aviso:#d29922;
    --acento:#58a6ff;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--fondo);color:var(--texto);
       font-family:"Segoe UI",system-ui,sans-serif;font-size:14px}
  header{display:flex;align-items:center;gap:14px;flex-wrap:wrap;
         padding:16px 22px;border-bottom:1px solid var(--borde);background:var(--caja)}
  h1{font-size:17px;margin:0;font-weight:600;letter-spacing:.3px}
  h1 span{color:var(--suave);font-weight:400}
  .pill{display:inline-flex;align-items:center;gap:8px;padding:6px 12px;border-radius:999px;
        font-weight:600;font-size:13px;border:1px solid var(--borde)}
  .pill .punto{width:9px;height:9px;border-radius:50%;background:var(--suave)}
  .pill.ok{color:var(--ok);border-color:#238636;background:#0f2a17}
  .pill.ok .punto{background:var(--ok);box-shadow:0 0 8px var(--ok)}
  .pill.mal{color:var(--mal);border-color:#8b2c2c;background:#2a1213}
  .pill.mal .punto{background:var(--mal)}
  .crece{flex:1}
  .mini{color:var(--suave);font-size:12px}
  main{padding:20px 22px 60px;max-width:1400px;margin:0 auto}
  .tarjetas{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}
  .card{background:var(--caja);border:1px solid var(--borde);border-radius:10px;padding:14px 16px}
  .card .rotulo{color:var(--suave);font-size:11px;text-transform:uppercase;letter-spacing:.8px}
  .card .valor{font-size:24px;font-weight:600;margin-top:6px}
  .card .valor.chico{font-size:15px;font-weight:500;word-break:break-all}
  .seccion{margin-top:22px}
  .seccion h2{font-size:13px;text-transform:uppercase;letter-spacing:.9px;color:var(--suave);
              margin:0 0 10px;font-weight:600}
  table{width:100%;border-collapse:collapse;background:var(--caja);border:1px solid var(--borde);
        border-radius:10px;overflow:hidden}
  th,td{padding:8px 10px;text-align:left;border-bottom:1px solid var(--borde);font-size:13px}
  th{color:var(--suave);font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.6px;
     background:#1b2129}
  tr:last-child td{border-bottom:none}
  td.num{font-variant-numeric:tabular-nums;color:var(--suave);white-space:nowrap}
  .cod{font-weight:700;font-variant-numeric:tabular-nums}
  .cod.ok{color:var(--ok)} .cod.mal{color:var(--mal)} .cod.aviso{color:var(--aviso)}
  .etiqueta{font-size:11px;padding:2px 8px;border-radius:999px;border:1px solid var(--borde);color:var(--suave)}
  .etiqueta.placa{color:var(--acento);border-color:#1f6feb;background:#0d1d33}
  .etiqueta.navegador{color:#a371f7;border-color:#6e40c9;background:#1c1230}
  pre{margin:0;background:#0b0f14;border:1px solid var(--borde);border-radius:10px;
      padding:12px 14px;max-height:380px;overflow:auto;font-family:Consolas,"Cascadia Mono",monospace;
      font-size:12px;line-height:1.55;white-space:pre-wrap;word-break:break-word}
  .ln{display:block}
  .ln .h{color:#6e7681;margin-right:8px}
  .ln.ERROR .t,.ln.CRITICAL .t{color:var(--mal)}
  .ln.WARNING .t,.ln.AVISO .t{color:var(--aviso)}
  .ln.INFO .t{color:#c9d1d9}
  .ln.SALIDA .t{color:#79c0ff}
  .ln .o{color:#6e7681}
  .vacio{color:var(--suave);font-style:italic;padding:12px 2px}
  .dos{display:grid;grid-template-columns:1fr 1fr;gap:18px}
  @media(max-width:900px){.dos{grid-template-columns:1fr}}
  .probar{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
  input[type=text]{flex:1;min-width:220px;background:#0b0f14;border:1px solid var(--borde);
       color:var(--texto);border-radius:8px;padding:9px 12px;font-size:13px}
  button{background:#21262d;border:1px solid var(--borde);color:var(--texto);border-radius:8px;
         padding:9px 14px;font-size:13px;cursor:pointer}
  button:hover{border-color:var(--acento);color:var(--acento)}
  button:disabled{opacity:.5;cursor:wait}
  a{color:var(--acento);text-decoration:none}
  .banner{display:none;margin:0 22px;padding:10px 14px;border-radius:8px;background:#2a1213;
          border:1px solid #8b2c2c;color:var(--mal);font-weight:600}
  .banner.visible{display:block}
  .detalle{color:var(--suave);font-size:12px;margin-top:2px}
  .pie{margin-top:26px;color:var(--suave);font-size:12px;line-height:1.7}
  code{background:#1b2129;padding:1px 6px;border-radius:5px;font-size:12px}
</style>
</head>
<body>
<header>
  <h1>Gisee <span>· panel de supervisión</span></h1>
  <div id="pastilla" class="pill"><span class="punto"></span><span id="pastillaTexto">conectando…</span></div>
  <div class="crece"></div>
  <div class="mini" id="uptime">—</div>
  <div class="mini"><a href="/docs" target="_blank">API (/docs)</a></div>
</header>
<div id="banner" class="banner">No se pudo contactar con el servidor. ¿Está levantado en el puerto 8000?</div>

<main>
  <div class="tarjetas">
    <div class="card"><div class="rotulo">Peticiones</div><div class="valor" id="kTotal">—</div></div>
    <div class="card"><div class="rotulo">Errores (5xx)</div><div class="valor" id="kErrores">—</div></div>
    <div class="card"><div class="rotulo">Avisos (4xx)</div><div class="valor" id="kAvisos">—</div></div>
    <div class="card"><div class="rotulo">Placa</div><div class="valor chico" id="kPlaca">—</div></div>
    <div class="card"><div class="rotulo">Último TTS</div><div class="valor chico" id="kTts">—</div></div>
    <div class="card"><div class="rotulo">Último chat de la placa</div><div class="valor chico" id="kChat">—</div></div>
  </div>

  <div class="seccion">
    <h2>Probar el servidor desde aquí</h2>
    <div class="probar">
      <input type="text" id="textoPrueba" value="Hola, soy Gisee. Estoy funcionando correctamente.">
      <button id="btnTts">Escuchar TTS</button>
      <button id="btnChat">Probar chat + voz</button>
      <span class="mini" id="pruebaEstado"></span>
    </div>
  </div>

  <div class="dos">
    <div class="seccion">
      <h2>Peticiones recientes</h2>
      <table>
        <thead><tr><th>Hora</th><th>Origen</th><th>Método</th><th>Ruta</th><th>Código</th><th>ms</th></tr></thead>
        <tbody id="tablaPeticiones"><tr><td colspan="6" class="vacio">Sin peticiones todavía.</td></tr></tbody>
      </table>
    </div>
    <div class="seccion">
      <h2>Errores y avisos</h2>
      <pre id="cajaErrores"><span class="vacio">Sin errores. </span></pre>
    </div>
  </div>

  <div class="seccion">
    <h2>Registro en vivo</h2>
    <pre id="cajaLogs"><span class="vacio">Esperando líneas…</span></pre>
  </div>

  <div class="pie">
    Se actualiza solo cada 2 segundos. Los datos también están en
    <code>logs/gisee.log</code> y en <code>/estado</code> (JSON) y <code>/logs</code> (texto).<br>
    Si esta página se queda en rojo, el servidor no está escuchando en el puerto 8000.
  </div>
</main>

<script>
const $ = (id) => document.getElementById(id);
let fallosSeguidos = 0;

function escapar(t) {
  return String(t == null ? "" : t)
    .replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;");
}

function claseCodigo(c) { return c >= 500 ? "mal" : (c >= 400 ? "aviso" : "ok"); }

function pintar(datos) {
  $("kTotal").textContent = datos.total_peticiones;
  $("kErrores").textContent = datos.errores;
  $("kAvisos").textContent = datos.avisos;
  $("kErrores").style.color = datos.errores ? "var(--mal)" : "";
  $("kAvisos").style.color = datos.avisos ? "var(--aviso)" : "";

  $("uptime").textContent = "activo desde " + datos.inicio + " · " + datos.uptime;

  if (datos.placa) {
    $("kPlaca").textContent = datos.placa.ip + " · " + datos.placa.hace;
    $("kPlaca").title = "Última petición: " + datos.placa.ruta + " (" + datos.placa.hora + ")";
  } else {
    $("kPlaca").textContent = "sin contacto";
  }

  $("kTts").textContent = datos.ultimo_tts ? datos.ultimo_tts.hace : "todavía no";
  $("kChat").textContent = datos.ultimo_chat
      ? datos.ultimo_chat.hora + " · " + datos.ultimo_chat.hace : "todavía no";

  const cuerpo = $("tablaPeticiones");
  if (!datos.peticiones.length) {
    cuerpo.innerHTML = '<tr><td colspan="6" class="vacio">Sin peticiones todavía.</td></tr>';
  } else {
    cuerpo.innerHTML = datos.peticiones.map(p => {
      const etiqueta = p.origen === "placa" ? "placa" : (p.origen === "panel" ? "panel" : "navegador");
      return `<tr>
        <td class="num">${p.hora}</td>
        <td><span class="etiqueta ${etiqueta}">${etiqueta}</span><div class="detalle">${escapar(p.cliente)}</div></td>
        <td>${p.metodo}</td>
        <td>${escapar(p.ruta)}</td>
        <td class="cod ${claseCodigo(p.estado)}">${p.estado}</td>
        <td class="num">${p.ms}</td>
      </tr>`;
    }).join("");
  }

  const problemas = datos.logs.filter(l => ["ERROR","CRITICAL","WARNING","AVISO"].includes(l.nivel));
  if (!problemas.length) {
    $("cajaErrores").innerHTML = '<span class="vacio">Sin errores ni avisos. </span>';
  } else {
    $("cajaErrores").innerHTML = problemas.slice(0, 40).map(l =>
      `<span class="ln ${l.nivel}"><span class="h">${l.hora}</span><span class="o">${escapar(l.origen)}</span> <span class="t">${escapar(l.texto)}</span>${l.detalle ? "\n" + escapar(l.detalle) : ""}</span>`
    ).join("");
  }

  const caja = $("cajaLogs");
  const pegadoAbajo = caja.scrollTop + caja.clientHeight >= caja.scrollHeight - 30;
  caja.innerHTML = datos.logs.slice(0, 250).reverse().map(l =>
    `<span class="ln ${l.nivel}"><span class="h">${l.hora}</span><span class="o">${escapar(l.origen)}</span> <span class="t">${escapar(l.texto)}</span>${l.detalle ? "\n" + escapar(l.detalle) : ""}</span>`
  ).join("") || '<span class="vacio">Esperando líneas…</span>';
  if (pegadoAbajo) caja.scrollTop = caja.scrollHeight;
}

async function refrescar() {
  try {
    const r = await fetch("/estado", { cache: "no-store" });
    if (!r.ok) throw new Error("HTTP " + r.status);
    const datos = await r.json();
    fallosSeguidos = 0;
    $("pastilla").className = "pill ok";
    $("pastillaTexto").textContent = "SERVIDOR ACTIVO · puerto " + datos.puerto;
    $("banner").className = "banner";
    pintar(datos);
  } catch (e) {
    fallosSeguidos++;
    if (fallosSeguidos >= 2) {
      $("pastilla").className = "pill mal";
      $("pastillaTexto").textContent = "SIN CONEXIÓN";
      $("banner").className = "banner visible";
    }
  }
}

async function reproducir(url, opciones) {
  const r = await fetch(url, opciones);
  if (!r.ok) {
    const texto = await r.text();
    throw new Error("HTTP " + r.status + ": " + texto.slice(0, 300));
  }
  const blob = await r.blob();
  const audio = new Audio(URL.createObjectURL(blob));
  await audio.play();
}

$("btnTts").onclick = async () => {
  $("btnTts").disabled = true;
  $("pruebaEstado").textContent = "generando voz…";
  try {
    await reproducir("/tts", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ texto: $("textoPrueba").value })
    });
    $("pruebaEstado").textContent = "voz reproducida en el navegador";
  } catch (e) {
    $("pruebaEstado").textContent = "falló: " + e.message;
  } finally {
    $("btnTts").disabled = false;
  }
};

$("btnChat").onclick = async () => {
  $("btnChat").disabled = true;
  $("pruebaEstado").textContent = "pensando y generando voz…";
  try {
    await reproducir("/chat/audio", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ MensajeUsuario: $("textoPrueba").value })
    });
    $("pruebaEstado").textContent = "respuesta reproducida en el navegador";
  } catch (e) {
    $("pruebaEstado").textContent = "falló: " + e.message;
  } finally {
    $("btnChat").disabled = false;
  }
};

refrescar();
setInterval(refrescar, 2000);
</script>
</body>
</html>
"""
