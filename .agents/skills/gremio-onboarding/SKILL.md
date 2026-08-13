---
name: gremio-onboarding
description: Checklist de todos los archivos a tocar en fontap-backend y fontap-app para dar de alta un gremio/oficio nuevo (ej. "jardinería avanzada") sin dejar ninguno desincronizado. Usar cuando se pide agregar/dar de alta un gremio, oficio o categoría profesional nueva a la plataforma. NO usar para crear un endpoint o modelo genérico suelto sin relación a gremios (usa backend-endpoint-scaffold) — esta skill es una checklist de orquestación entre los dos repos, no genera código por sí sola.
---

# Alta de gremio nuevo end-to-end

Un gremio nuevo toca ambos repos en varios puntos. Olvidar alguno deja el
gremio "a medias" (ej. se puede registrar un profesional con ese gremio
pero el mapa del frontend no le pone ícono, o el selector de registro ni
lo muestra). Esta skill es una checklist — para cada paso, usa la skill
específica correspondiente si existe, no reinventes el código a mano.

## Checklist completa

### Backend (fontap-backend) — este repo

1. **`app/main.py`**, constante `GREMIOS_VALIDOS` (o el nombre de la
   lista/enum equivalente en el momento de ejecutar esta skill — buscar con
   `grep -n "GREMIOS_VALIDOS" app/main.py`): agregar el valor nuevo del
   gremio, en minúsculas y sin espacios (ej. `jardineria_avanzada`).
2. **`app/schemas.py`**: si `UsuarioRegistro.gremio` usa un `Literal[...]`
   con la lista de gremios válidos (verificar con
   `grep -n "gremio: Literal" app/schemas.py`), agregar el valor ahí
   también — si no coincide con `GREMIOS_VALIDOS` del paso 1, el registro
   falla con un error de validación confuso en vez de uno claro.
3. Si el gremio nuevo necesita algún dato propio en el modelo (por ejemplo
   un campo específico del oficio), usar la skill `model-column-migration`
   para no olvidar `_migrar_columnas_faltantes`.

### Frontend (fontap-app) — repo hermano, no este

4. **`gremios.js`**: agregar la entrada al array `GREMIOS` con
   `{ valor, emoji, clave }` y el catálogo de servicios en
   `SERVICIOS_POR_GREMIO`.
5. **`screens/MapComponent.web.js`** y **`screens/MapComponent.native.js`**:
   confirmar que el emoji nuevo se resuelve en los pines del mapa.
6. **`screens/RegistroScreen.js`**: si el selector de gremio se genera
   dinámicamente desde `GREMIOS`, no hace falta tocar nada; si está
   hardcodeado, agregar la opción a mano.

Si esta sesión solo tiene clonado `fontap-backend`, los pasos 4-6 quedan
pendientes en el otro repo — avisar explícitamente que el alta queda
incompleta hasta hacerlos ahí, no darla por terminada solo con el lado
backend.

## Verificación final

Después de tocar todos los puntos alcanzables desde este repo, correr una
prueba rápida contra un backend local: registrar un profesional de prueba
con el gremio nuevo (ver skill `demo-data-seeder`, o un curl directo a
`/registro`) y confirmar que el registro no falla por validación.

## Qué NO hacer

No agregues el gremio solo en `GREMIOS_VALIDOS` "para probar después" sin
avisar del resto del checklist — la desincronización entre backend y
frontend es exactamente el tipo de bug que esta skill existe para evitar.
