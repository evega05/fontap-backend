---
name: backend-endpoint-scaffold
description: Genera un endpoint FastAPI nuevo en fontap-backend con Depends(get_db), Depends(auth.get_current_user) y verificación de propiedad, siguiendo el estilo ya establecido en app/main.py. Usar cuando se pide crear un endpoint/ruta nueva en el backend. NO usar para dar de alta un gremio completo (usa gremio-onboarding, que incluye pasos de backend Y frontend) ni para auditar endpoints existentes (usa orphan-endpoint-audit o id-convention-reviewer).
---

# Scaffolding de endpoint backend

## Patrón base (GET de un recurso propio)

```python
@app.get("/recurso/{recurso_id}", response_model=schemas.RecursoRespuesta)
def ver_recurso(
    recurso_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    recurso = db.query(models.Recurso).filter(models.Recurso.id == recurso_id).first()
    if not recurso:
        raise HTTPException(status_code=404, detail="Recurso no encontrado")
    if recurso.usuario_id != current_user["id"]:
        raise HTTPException(status_code=403, detail="No puedes ver este recurso")
    return recurso
```

## Patrón para crear (POST)

```python
@app.post("/recurso", response_model=schemas.RecursoRespuesta)
def crear_recurso(
    datos: schemas.RecursoCrear,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    nuevo = models.Recurso(usuario_id=current_user["id"], **datos.model_dump())
    db.add(nuevo)
    db.commit()
    db.refresh(nuevo)
    return nuevo
```

## La verificación de propiedad es la parte que MÁS se olvida

Todo endpoint que reciba un `{id}` en la URL y devuelva o modifique datos
de un usuario específico necesita comparar explícitamente contra
`current_user["id"]` (o resolver primero el `Fontanero` propio y comparar
contra su `id`/`usuario_id` real — ver skill `id-convention-reviewer` si
hay dudas de cuál corresponde). Sin este chequeo, cualquier usuario logueado
puede leer/modificar el recurso de otro con solo cambiar el número en la
URL (IDOR). Este proyecto ya tuvo varios bugs de este tipo corregidos en
otras sesiones — no los repitas.

Patrón de verificación reusable ya existente en el proyecto (buscar
`_verificar_fontanero_propio` o similar en `app/main.py` con
`grep -n "_verificar_fontanero_propio\|_verificar_participante" app/main.py`
para ver el helper exacto disponible antes de escribir uno nuevo).

## Antes de terminar

1. Si el endpoint necesita un `response_model`, confirmá que el schema
   correspondiente existe en `app/schemas.py` o creálo siguiendo el patrón
   de `class Config: from_attributes = True`.
2. Si el modelo tiene una columna nueva, usá la skill
   `model-column-migration` para no olvidar `_migrar_columnas_faltantes`.
3. Probá el endpoint con curl contra un backend local antes de darlo por
   terminado (ver skill `e2e-test-orchestrator` o simplemente un curl suelto
   si es un chequeo rápido).

## Qué NO hacer

No inventes un sistema de autenticación alternativo — todo pasa por
`Depends(auth.get_current_user)`, que ya valida el JWT. No devuelvas el
objeto SQLAlchemy crudo sin `response_model` si el modelo tiene campos
sensibles (`password_hash`, tokens) — usá siempre un schema Pydantic para
la respuesta.
