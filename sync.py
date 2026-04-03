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
from playwright.async_api import async_playwright, TimeoutError as PWTimeoutError

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
        writer.writerow(["imdbID", "Title", "Year", "WatchedDate", "Rating10"])

        for m in movies:
            watched_date = ""
            if m["watched_at"]:
                try:
                    dt = datetime.fromisoformat(m["watched_at"].replace("Z", "+00:00"))
                    watched_date = dt.strftime("%Y-%m-%d")
                except ValueError:
                    watched_date = m["watched_at"][:10]

            writer.writerow([
                m["imdb_id"],
                m["title"],
                m["year"],
                watched_date,
                "",  # Rating: Trakt requiere OAuth para leer ratings
            ])

    log.info(f"CSV generado: {output_path} ({len(movies)} películas)")
    return str(output_path), len(movies)


# ═══════════════════════════════════════════════════════════════════════════
#  MÓDULO 2: LETTERBOXD  →  subir CSV via Playwright
# ═══════════════════════════════════════════════════════════════════════════

async def upload_to_letterboxd(
    csv_path: str,
    username: str,
    password: str,
    headless: bool = True,
) -> None:
    """
    Abre Letterboxd.com, hace login y sube el CSV de importación.
    Usa Playwright con Chromium.
    """
    log.info("Iniciando Playwright para importar en Letterboxd…")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=headless,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",  # imprescindible en CI
            ],
        )

        # Contexto con User-Agent realista para no ser bloqueado
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
            locale="en-US",
        )
        page = await context.new_page()

        try:
            # ── PASO 1: Login ────────────────────────────────────────────
            log.info("Navegando a la página de login de Letterboxd…")
            await page.goto("https://letterboxd.com/sign-in/", wait_until="networkidle")

            # Rellenar formulario de login
            await page.fill('input[name="username"]', username)
            await page.fill('input[name="password"]', password)

            # Click en el botón de login
            await page.click('button[type="submit"]')

            # Esperar a que la página responda (Letterboxd puede redirigir a /username/ u otras URLs)
            await page.wait_for_load_state("networkidle", timeout=20_000)

            current_url = page.url
            log.info(f"URL tras login: {current_url}")

            # Si seguimos en sign-in → login falló
            if "sign-in" in current_url or "login" in current_url:
                error_el = page.locator(".flash-messages li, .error-text, [class*='error']")
                error_msg = ""
                if await error_el.count() > 0:
                    error_msg = (await error_el.first.text_content() or "").strip()
                raise LetterboxdLoginError(
                    f"Login fallido: usuario o contraseña incorrectos. "
                    f"Mensaje Letterboxd: '{error_msg}'. "
                    f"Verifica LETTERBOXD_USER y LETTERBOXD_PASS en los Secrets de GitHub."
                )

            log.info(f"✅ Login exitoso en Letterboxd (redirigió a: {current_url})")

            # Pequeña pausa para estabilidad
            await page.wait_for_timeout(2000)

            # ── PASO 2: Ir a la página de importación ───────────────────
            log.info("Navegando a la sección de importación…")
            await page.goto(LETTERBOXD_IMPORT_URL, wait_until="networkidle")

            # Verificar que estamos en la página correcta
            await page.wait_for_selector(
                'input[type="file"]', timeout=15_000
            )

            # ── PASO 3: Subir el archivo CSV ─────────────────────────────
            log.info(f"Subiendo CSV: {csv_path}")
            file_input = page.locator('input[type="file"]')
            await file_input.set_input_files(csv_path)

            # Esperar a que el archivo sea procesado (puede tardar)
            await page.wait_for_timeout(2000)

            # ── PASO 4: Confirmar la importación ─────────────────────────
            # Buscar el botón de "Import" o "Begin Import"
            import_btn = page.locator(
                'button:has-text("Import"), '
                'button:has-text("Begin import"), '
                'input[type="submit"][value*="Import"]'
            )

            if await import_btn.count() == 0:
                raise LetterboxdImportError(
                    "No se encontró el botón de importación. "
                    "Letterboxd puede haber cambiado su interfaz."
                )

            await import_btn.first.click()
            log.info("Importación enviada. Esperando confirmación…")

            # Esperar resultado (hasta 60 segundos)
            try:
                await page.wait_for_selector(
                    '.import-results, .import-complete, [class*="success"], h1:has-text("Import")',
                    timeout=60_000,
                )
                log.info("✅ Importación completada en Letterboxd")
            except PWTimeoutError:
                log.warning(
                    "Timeout esperando confirmación de importación. "
                    "Puede que haya funcionado de todos modos."
                )

            # Captura de pantalla del resultado (útil para debuggear en CI)
            screenshot_path = "/tmp/letterboxd_import_result.png"
            await page.screenshot(path=screenshot_path, full_page=True)
            log.info(f"📸 Captura guardada en {screenshot_path}")

        except (LetterboxdLoginError, LetterboxdImportError):
            # Re-lanzar errores de dominio
            await page.screenshot(path="/tmp/letterboxd_error.png", full_page=True)
            raise
        except Exception as e:
            await page.screenshot(path="/tmp/letterboxd_error.png", full_page=True)
            raise LetterboxdImportError(f"Error inesperado en Playwright: {e}") from e
        finally:
            await context.close()
            await browser.close()


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

    # HOURS_WINDOW: ventana de horas para el modo horario (por defecto 2h,
    # así si una ejecución falla, la siguiente recoge lo perdido)
    hours_window = int(os.getenv("HOURS_WINDOW", "2"))
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

    # ── Paso 3: Subir a Letterboxd via Playwright ────────────────────────
    await upload_to_letterboxd(
        csv_path=csv_path,
        username=required_vars["LETTERBOXD_USER"],
        password=required_vars["LETTERBOXD_PASS"],
        headless=headless,
    )

    log.info("🎉 ¡Sincronización completada exitosamente!")


if __name__ == "__main__":
    asyncio.run(main())
