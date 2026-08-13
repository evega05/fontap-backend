---
name: orphan-endpoint-audit
description: Cruza las rutas @app.get/post/put/delete de app/main.py (este repo, fontap-backend) contra las llamadas axios.get/post/put/delete de fontap-app, señalando endpoints del backend sin ningún consumidor en el frontend, o llamadas del frontend a rutas que no existen en el backend. Usar cuando se pregunta si hay endpoints sin usar, código muerto de API, o antes de una limpieza grande del backend. NO usar para revisar seguridad (usa la skill global security-review de Claude Code) ni para el bug específico de confundir Fontanero.id con usuario_id (usa id-convention-reviewer).
---

# Auditoría de endpoints huérfanos

## Cómo hacer la auditoría

Necesita ambos repos disponibles. Si en esta sesión solo está clonado
`fontap-backend`, avisar que el resultado será parcial (puede haber falsos
positivos de "endpoint sin consumidor" si el consumidor está en
`fontap-app`, que no está disponible para comparar).

### Paso 1: extraer todas las rutas de este repo (backend)

```bash
grep -oE '@app\.(get|post|put|delete)\("[^"]+"' app/main.py | sed 's/@app\.\w*("//;s/"$//' | sort -u
```

Normalizá los parámetros de path (`{servicio_id}`, `{fontanero_id}`, etc.)
a un placeholder genérico como `{id}` para poder comparar con las llamadas
del frontend, que arman la URL con template strings.

### Paso 2: extraer todas las llamadas del frontend (fontap-app)

Si el repo `fontap-app` está clonado en la sesión (verificar la ruta antes
de asumir), correr ahí:

```bash
grep -rohE "axios\.(get|post|put|delete)\(\`\\\$\{API\}[^,)\`]*" screens/*.js *.js | sed 's/axios\.\w*(`\${API}//'
```

Normalizá igual los interpolados (`${servicioId}`, `${userId}`, etc.) a
`{id}`.

### Paso 3: comparar

- **Rutas del backend sin match en el frontend**: candidatas a "huérfanas"
  — pero antes de reportarlas como muertas, verificá que no las use el
  panel admin (`app/static/admin.html`, que llama con `fetch` directo, no
  axios) ni un webhook externo (Google) que no pasa por el frontend.
- **Llamadas del frontend sin match en el backend**: son las más
  peligrosas — significa que una pantalla le pega a una ruta que ya no
  existe o nunca existió, y probablemente falla en producción ahora mismo.

## Formato del reporte

Tabla con: ruta, dónde se define (backend), dónde se consume (frontend, o
"ninguno encontrado"), y una nota si aplica alguna excepción (admin.html,
webhook).

## Qué NO hacer

No borres endpoints "huérfanos" automáticamente — a veces un endpoint
existe para un webhook o una versión de la app móvil que todavía no se
actualizó en el dispositivo de algún usuario. Reportá y dejá que el
usuario decida.
