---
name: id-convention-reviewer
description: Detecta el bug específico de confundir Fontanero.id con usuario_id en un parámetro {id} de una ruta de app/main.py, revisando que cada endpoint resuelva el id correcto antes de comparar contra current_user o hacer una query. Usar únicamente para este patrón puntual de confusión de IDs al tocar o revisar endpoints con {fontanero_id}/{usuario_id}/{id}. NO usar como revisión de seguridad general — para eso está la skill global security-review. NO usar para crear un endpoint nuevo desde cero (usa backend-endpoint-scaffold) ni para buscar endpoints sin uso (usa orphan-endpoint-audit).
---

# Revisor de convención de IDs (Fontanero.id vs usuario_id)

Este proyecto tiene dos IDs distintos que se parecen y se confunden fácil:

- **`Usuario.id`**: el id de la cuenta que inició sesión, el que viene en
  `current_user["id"]` después de `Depends(auth.get_current_user)`.
- **`Fontanero.id`**: el id de la fila en la tabla `fontaneros`, que tiene
  su propia columna `usuario_id` apuntando al usuario dueño. **No son el
  mismo número** — un profesional tiene un `Usuario.id` y un `Fontanero.id`
  distintos, generados en momentos distintos.

El bug real que este proyecto tuvo más de una vez: un endpoint recibe
`{fontanero_id}` en la URL, hace `db.query(models.Fontanero).get(fontanero_id)`,
y después compara mal — ya sea comparando `fontanero.id` contra
`current_user["id"]` (siempre falso, porque son namespaces de id distintos,
así que el profesional dueño no puede nunca pasar el chequeo), o al revés,
confiando en `fontanero_id` como si ya fuera el `usuario_id` del dueño y
saltándose la comparación de propiedad por completo (IDOR: cualquier
usuario logueado puede leer/tocar el fontanero de otro con solo cambiar el
número en la URL).

## Cómo revisar un endpoint

Para cada ruta con un parámetro de path que identifique un profesional o un
recurso suyo (`{fontanero_id}`, `{servicio_id}` de un servicio con
fontanero asociado, `{id}` ambiguo), verificar en orden:

1. **¿Qué representa el parámetro de la URL?** ¿Es un `Fontanero.id` o un
   `Usuario.id`? Mirar cómo se usa en la query (`models.Fontanero.id ==` vs
   `models.Usuario.id ==`) para confirmar, no asumir por el nombre del
   parámetro (`fontanero_id` a veces en código viejo del proyecto en
   realidad recibía un `usuario_id` por error de nombre).
2. **¿Se resuelve el objeto antes de comparar?** El patrón correcto es
   obtener la fila (`fontanero = db.query(models.Fontanero).filter(...).first()`)
   y comparar `fontanero.usuario_id == current_user["id"]` — nunca comparar
   directamente un `Fontanero.id` contra `current_user["id"]`.
3. **¿El 404 viene antes que el 403?** Si el recurso no existe, debe
   devolver 404 antes de intentar comparar propiedad (evita un
   `AttributeError` sobre `None`).

Patrón correcto de referencia:

```python
fontanero = db.query(models.Fontanero).filter(models.Fontanero.id == fontanero_id).first()
if not fontanero:
    raise HTTPException(status_code=404, detail="Fontanero no encontrado")
if fontanero.usuario_id != current_user["id"]:
    raise HTTPException(status_code=403, detail="No autorizado")
```

Buscar el helper ya existente en el proyecto antes de escribir la
comparación a mano:

```bash
grep -n "_verificar_fontanero_propio\|_verificar_participante" app/main.py
```

## Cómo encontrar candidatos a revisar

```bash
grep -n "fontanero_id: int\|def .*(.*_id: int" app/main.py | head -50
```

Para cada resultado, aplicar los 3 chequeos de arriba. Priorizar endpoints
que modifican datos (PUT/POST/DELETE) sobre los de solo lectura (GET),
porque el impacto de un bug ahí es mayor.

## Formato del reporte

Lista de endpoints revisados con: ruta, qué id espera, si la comparación de
propiedad está bien hecha (sí/no/no aplica — ej. endpoints públicos como el
perfil visible en el mapa no necesitan esta comparación), y el fix concreto
si falta.

## Qué NO hacer

No asumas que todos los `{id}` del proyecto son iguales — revisá cada uno
contra la query real que lo usa. No hagas un refactor grande para unificar
los dos tipos de id en uno solo salvo que el usuario lo pida explícitamente
— es un cambio de modelo de datos grande, esta skill es solo para detectar
y corregir el bug de comparación puntual.
