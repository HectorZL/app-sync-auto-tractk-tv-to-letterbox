"""
trakt-to-letterboxd sync
========================
Descarga el historial de películas Y series vistas en Trakt y las importa
automáticamente a Letterboxd usando Playwright (navegador automatizado).

Modo "cuasi tiempo real":
  Ejecutado cada hora por GitHub Actions (cron: "0 * * * *").
  Cada ejecución solo mira las últimas HOURS_WINDOW horas → máximo 1h de retraso.

Variables de entorno requeridas (GitHub Secrets):
  TRAKT_CLIENT_ID    → Client ID de tu app en trakt.tv/oauth/applications
  TRAKT_USERNAME     → Tu nombre de usuario en Trakt
  LETTERBOXD_USER    → Tu nombre de usuario en Letterboxd
  LETTERBOXD_PASS    → Tu contraseña de Letterboxd

Opcional:
  DAYS_BACK          → Días hacia atrás (modo manual). Default 1.
  HOURS_WINDOW       → Horas hacia atrás (modo automático/hourly). Default 2.
  HEADLESS           → "true" para modo invisible (default en CI)
  SYNC_SHOWS         → "true" para incluir series además de películas (default: true)
"""

import os
import csv
import sys
import time
import logging
import tempfile
import asyncio
from datetime import datetime, timezone, timedelta
from pathlib import Path

import httpx
from seleniumbase import SB
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # en Github Actions no hará ni falta


# ─── Configuración de logging ───────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ─── Constantes ─────────────────────────────────────────────────────────────
TRAKT_API_BASE = "https://api.trakt.tv"
LETTERBOXD_IMPORT_URL = "https://letterboxd.com/import/"
TRAKT_API_VERSION = "2"

# ─── Clases de error personalizadas ─────────────────────────────────────────
class TraktAPIError(Exception):
    pass

class LetterboxdLoginError(Exception):
    pass

class LetterboxdImportError(Exception):
    pass


# ═══════════════════════════════════════════════════════════════════════════
#  MÓDULO 1: TRAKT  →  obtener historial (películas + series)
# ═══════════════════════════════════════════════════════════════════════════

async def _fetch_history(
    client_id: str,
    username: str,
    media_type: str,   # "movies" o "shows"
    start_dt: datetime,
    end_dt: datetime,
) -> list[dict]:
    """
    Función interna: llama a /users/{user}/history/{type} con paginación.
    Devuelve lista de dicts con imdb_id, title, year, watched_at, media_type.
    """
    headers = {
        "Content-Type": "application/json",
        "trakt-api-version": TRAKT_API_VERSION,
        "trakt-api-key": client_id,
    }
    start_str = start_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    end_str = end_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    items: list[dict] = []
    page = 1
    per_page = 100

    async with httpx.AsyncClient(timeout=30.0) as client:
        while True:
            url = (
                f"{TRAKT_API_BASE}/users/{username}/history/{media_type}"
                f"?start_at={start_str}&end_at={end_str}"
                f"&page={page}&limit={per_page}"
            )
            response = await client.get(url, headers=headers)

            if response.status_code == 401:
                raise TraktAPIError("Trakt API key inválida o usuario privado.")
            if response.status_code == 404:
                raise TraktAPIError(f"Usuario '{username}' no encontrado en Trakt.")
            if response.status_code != 200:
                raise TraktAPIError(
                    f"Error Trakt API [{media_type}]: {response.status_code} — {response.text}"
                )

            data = response.json()
            if not data:
                break

            key = "movie" if media_type == "movies" else "show"
            for item in data:
                entry = item.get(key, {})
                ids = entry.get("ids", {})
                items.append({
                    "imdb_id": ids.get("imdb", ""),
                    "tmdb_id": ids.get("tmdb", ""),
                    "title": entry.get("title", ""),
                    "year": entry.get("year", ""),
                    "watched_at": item.get("watched_at", ""),
                    "media_type": key,   # "movie" o "show"
                })

            total_pages = int(response.headers.get("X-Pagination-Page-Count", 1))
            log.info(f"  [{media_type}] Página {page}/{total_pages} — {len(data)} entradas")
            if page >= total_pages:
                break
            page += 1

    return items


async def fetch_trakt_history(
    client_id: str,
    username: str,
    hours_window: int = 2,
    sync_shows: bool = True,
) -> list[dict]:
    """
    Descarga lo visto en Trakt en las últimas `hours_window` horas.
    Incluye películas y (opcionalmente) series.
    Letterboxd solo importa películas, pero la info de series puede servir
    de referencia futura o para herramientas como Serializd.
    """
    end_dt = datetime.now(tz=timezone.utc)
    start_dt = end_dt - timedelta(hours=hours_window)

    log.info(
        f"⏱  Ventana de sincronización: últimas {hours_window}h "
        f"({start_dt.strftime('%H:%M')} → {end_dt.strftime('%H:%M')} UTC)"
    )

    # Películas (siempre)
    movies = await _fetch_history(client_id, username, "movies", start_dt, end_dt)
    log.info(f"🎬 Películas encontradas: {len(movies)}")

    shows: list[dict] = []
    if sync_shows:
        shows = await _fetch_history(client_id, username, "shows", start_dt, end_dt)
        log.info(f"📺 Series encontradas   : {len(shows)}")

    all_items = movies + shows

    # Deduplicar por imdb_id + media_type
    seen: set[str] = set()
    unique: list[dict] = []
    for item in all_items:
        key = f"{item['media_type']}:{item['imdb_id'] or item['title']}"
        if key not in seen:
            seen.add(key)
            unique.append(item)

    log.info(f"✅ Total entradas únicas: {len(unique)} ({len(movies)} películas + {len(shows)} series)")
    return unique


def build_csv(items: list[dict]) -> str:
    """
    Genera el CSV en el formato que acepta Letterboxd.
    Letterboxd solo importa películas (media_type="movie"), las series se omiten
    en el CSV principal pero se loguean por separado.

    Formato: https://letterboxd.com/about/importing-data/
    Columnas: imdbID, Title, Year, WatchedDate, Rating10
    """
    output_path = Path(tempfile.mkdtemp()) / "trakt_export.csv"

    movies = [i for i in items if i["media_type"] == "movie"]
    shows  = [i for i in items if i["media_type"] == "show"]

    if shows:
        log.info(f"📺 Series detectadas (no se importan a Letterboxd, que solo acepta películas):")
        for s in shows:
            log.info(f"   • {s['title']} ({s['year']})")

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Title", "Year", "WatchedDate", "Rating10", "IMDb URI"])

        for m in movies:
            watched_date = ""
            if m["watched_at"]:
                try:
                    dt = datetime.fromisoformat(m["watched_at"].replace("Z", "+00:00"))
                    watched_date = dt.strftime("%Y-%m-%d")
                except ValueError:
                    watched_date = m["watched_at"][:10]

            imdb_url = f"https://www.imdb.com/title/{m['imdb_id']}/" if m.get("imdb_id") else ""

            writer.writerow([
                m["title"],
                m["year"],
                watched_date,
                "",  # Rating: Trakt requiere OAuth para leer ratings
                imdb_url,
            ])

    log.info(f"CSV generado: {output_path} ({len(movies)} películas)")
    return str(output_path), len(movies)


# ═══════════════════════════════════════════════════════════════════════════
#  MÓDULO 2: LETTERBOXD  →  subir CSV via Playwright
# ═══════════════════════════════════════════════════════════════════════════

def upload_to_letterboxd(
    csv_path: str,
    username: str,
    password: str,
    headless: bool = True,
) -> None:
    """
    Abre Letterboxd.com, inyecta sesión y sube el CSV de importación.
    Usa SeleniumBase UC Mode para aplastar el captcha de Cloudflare.
    """
    log.info("Iniciando SeleniumBase UC Mode para burlar Cloudflare…")

    import os
    csrf_cookie = os.getenv("LETTERBOXD_COOKIE_CSRF")
    user_cookie = os.getenv("LETTERBOXD_COOKIE_CURRENT")

    # UC Mode (Undetected Chromedriver) necesita headless=False normalmente para funcionar al 100%
    # pero en Linux/GitHub Actions podemos usar xvfb junto con headless=False
    # SB lo gestiona inteligentemente con el parámetro uc=True.
    with SB(uc=True, headless=headless, xvfb=True) as sb:
        try:
            log.info("Navegando inicialmente a Letterboxd...")
            sb.open("https://letterboxd.com/")
            sb.sleep(2)
            
            if csrf_cookie and user_cookie:
                log.info("🔑 Cookies mágicas detectadas. Inyectando sesión...")
                sb.add_cookie({"name": "com.xk72.webparts.csrf", "value": csrf_cookie, "domain": ".letterboxd.com", "path": "/"})
                sb.add_cookie({"name": "letterboxd.user.CURRENT", "value": user_cookie, "domain": ".letterboxd.com", "path": "/"})
                sb.sleep(1)
            else:
                log.info("No hay cookies configuradas. Intentando login tradicional con usuario/pass...")
                sb.open("https://letterboxd.com/sign-in/")
                sb.type('input[name="username"]', username)
                sb.type('input[name="password"]', password)
                sb.click('.button-action[type="submit"], input[type="submit"], button[type="submit"]')
                sb.sleep(5)
                
                current_url = sb.get_current_url()
                if "sign-in" in current_url or "login" in current_url:
                    raise LetterboxdLoginError(
                        "Login fallido: Cloudflare bloqueó el robot o clave incorrecta. "
                        "Por favor configura LETTERBOXD_COOKIE_CSRF y LETTERBOXD_COOKIE_CURRENT en los Secrets."
                    )
                log.info(f"✅ Login clásico exitoso en Letterboxd (redirigió a: {current_url})")

            # ── PASO 2: Ir a la página de importación ───────────────────
            log.info("Navegando a la sección de importación…")
            sb.open(LETTERBOXD_IMPORT_URL)
            sb.sleep(3)

            # ── PASO 3+4: Subir CSV nativamente y clickear "Save" ──────────
            log.info(f"Subiendo CSV form input de Letterboxd: {csv_path}")
            
            # Subir archivo al input file nativo de la web
            sb.choose_file('input[type="file"]', csv_path)
            sb.sleep(3)

            # A veces hay que hacer click en el botón de Importar luego de seleccionar
            try:
                sb.click('button:contains("Import"), input[type="submit"]', timeout=3)
            except Exception:
                pass
            
            sb.sleep(5)
            log.info("Página de revisión de importación completada.")

            # Esperar a que la página de Review nos muestre el botón de confirmación ("Save X films", "Save", etc)
            try:
                # El botón final suele tener una clase accionable con "Save"
                sb.wait_for_element('button[class*="action"]:contains("Save"), a.button-action:contains("Save")', timeout=15)
                sb.click('button[class*="action"]:contains("Save"), a.button-action:contains("Save")')
                log.info("✅ Botón 'Save' final clickeado correctamente.")
                sb.sleep(5)
            except Exception:
                log.warning(
                    "Timeout esperando confirmación visual de 'Save'. "
                    "Puede que el botón tenga un nombre distinto o Turnstile escondió el DOM. Revisa la captura manual."
                )

            # Captura de pantalla del resultado
            screenshot_path = "/tmp/letterboxd_import_result.png"
            # Aseguramos que la carpeta existe en local
            os.makedirs(os.path.dirname(screenshot_path), exist_ok=True)
            sb.save_screenshot(screenshot_path)
            log.info(f"📸 Captura guardada en {screenshot_path}")

        except (LetterboxdLoginError, LetterboxdImportError):
            sb.save_screenshot("/tmp/letterboxd_error.png")
            raise
        except Exception as e:
            sb.save_screenshot("/tmp/letterboxd_error.png")
            raise LetterboxdImportError(f"Error inesperado en SeleniumBase: {e}") from e


# ═══════════════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════════════

async def main() -> None:
    # ── Leer variables de entorno ────────────────────────────────────────
    required_vars = {
        "TRAKT_CLIENT_ID": os.getenv("TRAKT_CLIENT_ID"),
        "TRAKT_USERNAME": os.getenv("TRAKT_USERNAME"),
        "LETTERBOXD_USER": os.getenv("LETTERBOXD_USER"),
        "LETTERBOXD_PASS": os.getenv("LETTERBOXD_PASS"),
    }

    missing = [k for k, v in required_vars.items() if not v]
    if missing:
        log.error(
            f"❌ Faltan variables de entorno requeridas: {', '.join(missing)}\n"
            "   Agrégalas en GitHub → Settings → Secrets → Actions"
        )
        sys.exit(1)

    # HOURS_WINDOW: ventana de horas para el modo horario
    hw_env = os.getenv("HOURS_WINDOW", "")
    hours_window = int(hw_env) if hw_env.strip() else 2
    headless     = os.getenv("HEADLESS",    "true").lower() == "true"
    sync_shows   = os.getenv("SYNC_SHOWS",  "true").lower() == "true"

    log.info("=" * 60)
    log.info("  TRAKT → LETTERBOXD  Sync  (modo: cada hora)  ")
    log.info("=" * 60)
    log.info(f"Usuario Trakt     : {required_vars['TRAKT_USERNAME']}")
    log.info(f"Usuario Letterboxd: {required_vars['LETTERBOXD_USER']}")
    log.info(f"Ventana de tiempo : últimas {hours_window} horas")
    log.info(f"Sincronizar series: {sync_shows}")
    log.info(f"Modo headless     : {headless}")
    log.info("=" * 60)

    # ── Paso 1: Descargar lo visto en Trakt (películas + series) ─────────
    all_items = await fetch_trakt_history(
        client_id=required_vars["TRAKT_CLIENT_ID"],
        username=required_vars["TRAKT_USERNAME"],
        hours_window=hours_window,
        sync_shows=sync_shows,
    )

    if not all_items:
        log.info("⏭  Sin actividad nueva en Trakt. Saltando importación (0 minutos de CI gastados en Playwright).")
        return

    # ── Paso 2: Generar CSV (solo películas van a Letterboxd) ────────────
    csv_path, movie_count = build_csv(all_items)

    if movie_count == 0:
        log.info("⏭  Solo hay series nuevas (sin películas). Letterboxd no necesita actualización.")
        return

    # ── Paso 3: Subir a Letterboxd via SeleniumBase ────────────────────────
    await asyncio.to_thread(
        upload_to_letterboxd,
        csv_path,
        required_vars["LETTERBOXD_USER"],
        required_vars["LETTERBOXD_PASS"],
        headless
    )

    log.info("🎉 ¡Sincronización completada exitosamente!")


if __name__ == "__main__":
    asyncio.run(main())
