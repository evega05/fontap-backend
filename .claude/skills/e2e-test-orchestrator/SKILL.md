---
name: e2e-test-orchestrator
description: Levanta un backend local (venv + sqlite) de este repo (fontap-backend) y, si fontap-app también está clonado en la sesión, un frontend local (expo start --web --offline) apuntándole, navegando el flujo con Playwright para verificar una feature completa de punta a punta, limpiando todo al terminar. Usar cuando hay que probar visualmente que una feature grande funciona de verdad (no solo revisar el código), especialmente antes de dar una tarea por terminada. NO usar para una prueba rápida de un solo endpoint sin frontend (usa payment-flow-tester, mucho más rápido, solo curl).
---

# Orquestador de prueba end-to-end (desde fontap-backend)

Este es el flujo completo, ya afinado a base de errores reales cometidos en
este proyecto — seguilo tal cual, especialmente la parte de limpieza, que
tiene una lección de un bug real aprendida a los golpes (ver el aviso al
final).

## Paso 1: backend local (este repo)

```bash
python3 -m venv .venv 2>/dev/null
.venv/bin/pip install -q -r requirements.txt uvicorn

PUERTO_BACKEND=8977  # elegir uno libre, no reusar puertos de pruebas previas en la misma sesión
rm -f /tmp/fontap_e2e.db /tmp/uvicorn_e2e.log
DATABASE_URL="sqlite:////tmp/fontap_e2e.db" DESACTIVAR_RECORDATORIOS=1 \
  nohup .venv/bin/python -m uvicorn app.main:app --port $PUERTO_BACKEND > /tmp/uvicorn_e2e.log 2>&1 &
sleep 3
curl -s http://127.0.0.1:$PUERTO_BACKEND/  # debe responder {"mensaje":"..."}
```

Si falla con `ModuleNotFoundError` para `cryptography`/`jose`, es el
conflicto conocido con el paquete de sistema — usar el venv
(`.venv/bin/python`) en vez de `python3` a secas resuelve esto siempre.

Sembrá los usuarios/datos de prueba que necesites con curl directo a
`/registro`, `/login`, etc. (o usa la skill `demo-data-seeder` si necesitás
varios profesionales realistas).

## Paso 2: ¿hace falta frontend?

Si la feature a probar es solo de backend (lógica de negocio, respuesta de
un endpoint), no sigas al frontend — un curl directo alcanza. Si hace falta
ver la pantalla de verdad, seguí solo si el repo `fontap-app` también está
clonado en esta sesión (verificar la ruta antes de asumir que existe); si
no está, avisar que la prueba visual completa no es posible desde acá y
ofrecer probar solo el backend con curl como alternativa parcial.

Con `fontap-app` disponible:

```bash
cd ../fontap-app  # o la ruta real donde esté clonado
npm install --silent

# Apuntar temporalmente TODOS los archivos con la URL de producción al backend local.
# GUARDAR la lista exacta de archivos tocados en un archivo — se necesita
# después para revertir sin depender de `git status` (ver aviso más abajo).
grep -rl "https://fontap-backend-production.up.railway.app" --include="*.js" . > /tmp/e2e_archivos_sed.txt
while IFS= read -r f; do
  sed -i.bak "s|https://fontap-backend-production.up.railway.app|http://127.0.0.1:$PUERTO_BACKEND|g" "$f"
done < /tmp/e2e_archivos_sed.txt

PUERTO_FRONTEND=8984  # elegir uno libre
rm -f /tmp/expo_e2e.log
EXPO_OFFLINE=1 nohup npx expo start --web --port $PUERTO_FRONTEND --offline > /tmp/expo_e2e.log 2>&1 &
for i in $(seq 1 20); do
  code=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:$PUERTO_FRONTEND)
  [ "$code" = "200" ] && break
  sleep 2
done
```

El flag `--offline` es obligatorio en este entorno: sin él, el CLI de Expo
falla con `TypeError: Body is unusable: Body has already been read`.

## Paso 3: navegar con Playwright

Si `playwright` no está disponible, instalalo en un directorio de scratch
(no en ningún repo):

```bash
cd /tmp && npm install --silent playwright@1.56.1
```

Lanzalo siempre con el Chromium preinstalado del entorno, nunca descargues
uno nuevo:

```js
const browser = await chromium.launch({ executablePath: '/opt/pw-browsers/chromium', headless: true });
```

Sacá capturas de pantalla en cada paso relevante para verificar visualmente
el resultado, no solo confiar en que el script no tiró error.

## Paso 4: limpieza — el paso que NO se puede saltar

**Aviso de un incidente real en este proyecto**: al restaurar archivos
desde `.bak`, un script que parseaba `git status --short` para encontrar
qué mover falló silenciosamente en archivos con `ñ` en el nombre
(`ReseñaScreen.js`), porque git escapa esos caracteres en su output
porcelain, y esos archivos quedaron con la URL de prueba puesta —casi se
commitea así—. Por eso: **restaurá siempre desde la lista guardada en el
paso 2 (`/tmp/e2e_archivos_sed.txt`), nunca parseando `git status`.**

```bash
# matar procesos
pkill -f "uvicorn app.main:app --port $PUERTO_BACKEND" 2>/dev/null
pkill -f "expo start.*--port $PUERTO_FRONTEND" 2>/dev/null
sleep 1

# restaurar URLs de producción desde la lista guardada, NO desde git status (en fontap-app)
while IFS= read -r f; do
  if [ -f "$f.bak" ]; then mv "$f.bak" "$f"; else echo "AVISO: falta .bak de $f"; fi
done < /tmp/e2e_archivos_sed.txt

# verificar que no quedó ningún resto
grep -rl "127.0.0.1:$PUERTO_FRONTEND\|127.0.0.1:$PUERTO_BACKEND" --include="*.js" . && echo "¡QUEDARON RESTOS, revisar!"
find . -name "*.bak" -not -path "./node_modules/*"

# borrar artefactos de prueba
rm -f /tmp/fontap_e2e.db /tmp/uvicorn_e2e.log /tmp/expo_e2e.log /tmp/e2e_archivos_sed.txt
```

**Si editaste código de verdad (no solo probaste) mientras el sed de prueba
estaba activo**, esos cambios reales quedaron mezclados en los mismos
archivos que el `.bak` va a sobrescribir — el `mv "$f.bak" "$f"` restaura
TODO el archivo a como estaba antes del sed, incluyendo tus ediciones
reales. Si esto pasa: rehacé esas ediciones después de la restauración, no
antes — nunca edites contenido real mientras la URL de prueba sigue puesta,
o hacé el chequeo `git diff` archivo por archivo antes de dar la tarea por
terminada para confirmar que no se perdió nada.

## Qué NO hacer

No dejes el backend/frontend de prueba corriendo en background al terminar
la tarea. No hagas commit con las URLs de prueba puestas — verificá
`git status`/`git diff` después de la limpieza, no antes.
