# 🎬 Trakt → Letterboxd Auto-Sync

Robot de sincronización perpetuo: descarga automáticamente tu historial de películas de **Trakt.tv** y las importa a **Letterboxd** usando GitHub Actions + Playwright.

**Ejecuta solo una vez la configuración y nunca más tendrás que registrar tus películas a mano.**

---

## ¿Cómo funciona?

```
GitHub Actions (Cron)
       │
       ▼
  sync.py se ejecuta
       │
       ├─► Llama a la API de Trakt
       │         └─► Descarga películas vistas (últimos 7 días)
       │
       ├─► Genera un archivo CSV con formato de Letterboxd
       │
       └─► Abre Chrome "fantasma" (Playwright)
                 ├─► Hace login en letterboxd.com
                 └─► Sube el CSV en la sección de importación
```

---

## ⚡ Configuración (5 pasos)

### Paso 1 — Crea una app en Trakt

1. Ve a [trakt.tv/oauth/applications](https://trakt.tv/oauth/applications)
2. Haz clic en **"New Application"**
3. Pon cualquier nombre (ej: `my-sync`)
4. En **Redirect URIs** escribe `urn:ietf:wg:oauth:2.0:oob`
5. Guarda y copia el **Client ID**

### Paso 2 — Sube este repo a GitHub

```bash
git init
git add .
git commit -m "feat: trakt-to-letterboxd sync"
git remote add origin https://github.com/TU_USUARIO/trakt-letterboxd-sync.git
git push -u origin main
```

### Paso 3 — Configura los Secrets en GitHub

En tu repositorio:  
**Settings → Secrets and variables → Actions → New repository secret**

| Secret              | Valor                               |
|---------------------|-------------------------------------|
| `TRAKT_CLIENT_ID`   | El Client ID de tu app en Trakt     |
| `TRAKT_USERNAME`    | Tu nombre de usuario en Trakt       |
| `LETTERBOXD_USER`   | Tu usuario en Letterboxd            |
| `LETTERBOXD_PASS`   | Tu contraseña de Letterboxd         |

> ⚠️ **Nunca** pongas estas claves directamente en el código.

### Paso 4 — Activa el Workflow

1. Ve a la pestaña **Actions** de tu repositorio
2. Si ves un aviso amarillo, haz clic en **"I understand my workflows, go ahead and enable them"**
3. Listo. Se ejecutará automáticamente todos los días a medianoche UTC.

### Paso 5 — Prueba manual (opcional)

En la pestaña **Actions** → elige **"Trakt → Letterboxd Sync"** → **"Run workflow"**  
Puedes especificar cuántos días hacia atrás sincronizar (ej: `30` para importar el último mes).

---

## 🗓️ Ajustar el horario

Edita la línea `cron` en `.github/workflows/sync.yml`:

```yaml
# Ejemplos:
- cron: "0 0 * * *"   # Todos los días a medianoche UTC
- cron: "0 6 * * *"   # Todos los días a las 6 AM UTC
- cron: "0 0 * * 1"   # Sólo los lunes
- cron: "0 0 1 * *"   # El primero de cada mes
```

Usa [crontab.guru](https://crontab.guru) para generar tu expresión cron.

---

## 🔍 Monitoreo y depuración

- **¿Falló?** GitHub te enviará un email automáticamente.
- **Ver logs:** Pestaña Actions → clic en la ejecución fallida → clic en el job "Sincronizar películas"
- **Capturas de pantalla:** Si falla, se sube automáticamente una captura de pantalla del navegador como artefacto (visible 7 días).

---

## 📁 Estructura del proyecto

```
.
├── sync.py                        # Script principal de Python
├── requirements.txt               # Dependencias Python
├── .github/
│   └── workflows/
│       └── sync.yml               # Definición del Action (cron, pasos, secrets)
└── README.md
```

---

## ⚙️ Variables de entorno

| Variable          | Obligatoria | Default | Descripción                              |
|-------------------|-------------|---------|------------------------------------------|
| `TRAKT_CLIENT_ID` | ✅ Sí       | —       | Client ID de tu app en Trakt             |
| `TRAKT_USERNAME`  | ✅ Sí       | —       | Tu usuario en Trakt                      |
| `LETTERBOXD_USER` | ✅ Sí       | —       | Tu usuario en Letterboxd                 |
| `LETTERBOXD_PASS` | ✅ Sí       | —       | Tu contraseña de Letterboxd              |
| `DAYS_BACK`       | ❌ No       | `7`     | Días hacia atrás a sincronizar           |
| `HEADLESS`        | ❌ No       | `true`  | `false` para ver el navegador (local)    |

---

## 🔒 Seguridad

- Las credenciales se leen **exclusivamente** de variables de entorno / GitHub Secrets.
- El código fuente no contiene ninguna contraseña ni API key.
- El repositorio puede ser público sin riesgo.

---

## 🛠️ Ejecutar localmente (desarrollo)

```bash
# 1. Crear entorno virtual
python -m venv venv
venv\Scripts\activate        # Windows
# source venv/bin/activate   # macOS/Linux

# 2. Instalar dependencias
pip install -r requirements.txt
playwright install chromium

# 3. Configurar variables de entorno
set TRAKT_CLIENT_ID=tu_client_id_aqui
set TRAKT_USERNAME=tu_usuario_trakt
set LETTERBOXD_USER=tu_usuario_letterboxd
set LETTERBOXD_PASS=tu_contraseña_letterboxd
set DAYS_BACK=3
set HEADLESS=false          # false = ver el navegador abrirse (útil para debug)

# 4. Ejecutar
python sync.py
```

---

## 💡 Preguntas frecuentes

**¿Cuántos minutos de GitHub Actions gasta?**  
Aproximadamente 3-5 minutos por ejecución. Con el plan gratuito (2000 min/mes) te sobra incluso ejecutándolo a diario.

**¿Qué pasa si Letterboxd ya tiene la película?**  
Letterboxd detecta duplicados durante la importación y los omite automáticamente.

**¿Importa también las calificaciones de Trakt?**  
Actualmente no (Trakt requiere OAuth para leer ratings privados). Solo importa el registro de "visto" con la fecha.

**¿Puede dejar de funcionar?**  
Si Letterboxd cambia el diseño de su página de importación, Playwright puede fallar al no encontrar los botones. GitHub te avisará por email y podrás ajustar los selectores en `sync.py`.
