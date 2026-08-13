---
name: demo-data-seeder
description: Crea profesionales y clientes de prueba con datos realistas (nombre, gremio, zona, servicios, reseñas) contra un backend local de fontap-backend, para poder navegar y probar pantallas del frontend sin registrar todo manualmente cada vez. Usar cuando se necesitan varios usuarios de prueba con datos ya poblados para ver cómo se ve una pantalla con contenido real. NO usar para probar la corrección de un flujo específico paso a paso (usa payment-flow-tester para pagos, o e2e-test-orchestrator para una feature completa).
---

# Sembrador de datos demo

Ya existe un script de referencia en el repo:
`scripts/seed_profesionales_demo.py` — revisalo primero
(`cat scripts/seed_profesionales_demo.py`) antes de escribir uno nuevo, es
probable que ya cubra la mayoría de lo que necesitás o sirva de plantilla
directa.

## Qué poblar para una demo realista

Un set mínimo útil para probar la mayoría de pantallas del frontend:

- **3-5 profesionales** de gremios distintos (electricista, fontanero,
  pintor...), cada uno con: zona (`Bilbao`, `Madrid`, etc.), al menos 2
  servicios en su catálogo (`POST /gestion/...` o el endpoint de servicios
  del fontanero), y ubicación (`latitud`/`longitud`) para que aparezcan en
  el mapa.
- **1-2 clientes** de prueba.
- **Reseñas**: para que un profesional muestre valoración en el mapa/perfil
  público, necesita al menos un servicio completado y pagado con una
  reseña asociada — no se puede setear `valoracion` directo en la tabla sin
  pasar por el flujo real (ver skill `payment-flow-tester` para el ciclo
  completo, y después `POST /servicios/{id}/resena`).
- **Una empresa con equipo**, si vas a probar el Panel de gestión o
  Administración: un profesional con `nombre_empresa` puesto
  (`PUT /fontaneros/{id}/empresa`) y 1-2 empleados invitados/aceptados.

## Patrón de script

```python
import requests

API = "http://127.0.0.1:8977"  # ajustar al puerto del backend local

profesionales = [
    {"nombre": "Ana García", "email": "ana@demo.com", "gremio": "electricista", "zona": "Bilbao"},
    {"nombre": "Carlos Ruiz", "email": "carlos@demo.com", "gremio": "fontanero", "zona": "Bilbao"},
    # ...
]

for p in profesionales:
    requests.post(f"{API}/registro", json={
        "nombre": p["nombre"], "email": p["email"], "telefono": "600000000",
        "password": "demo1234", "tipo": "fontanero", "gremio": p["gremio"],
        "terminos_aceptados": True,
    })
    # login, poner ubicación, agregar servicios, etc. — ver seed_profesionales_demo.py
    # para el patrón exacto de las llamadas siguientes.
```

## Fotos de perfil

Este proyecto no tiene fotos de stock incluidas — si necesitás profesionales
con foto de perfil real para una demo visual, subilas manualmente vía
`POST /fontaneros/{id}/foto` con un archivo de imagen, o dejalo sin foto
(la app muestra un avatar con la inicial del nombre por defecto, que es
aceptable para la mayoría de pruebas).

## Qué NO hacer

No uses este sembrador contra el backend de producción (Railway) — es
exclusivamente para un backend local levantado para pruebas. Verificá
siempre que `API` apunte a `127.0.0.1` antes de correr el script.
