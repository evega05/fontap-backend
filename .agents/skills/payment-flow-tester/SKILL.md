---
name: payment-flow-tester
description: Script curl que recorre el ciclo completo de un servicio contra un backend local de fontap-backend (crear solicitud → poner precio → aceptar precio → completar → pagar en efectivo o Bizum → confirmar por el profesional), sin frontend ni navegador. Usar para probar rápido un cambio de backend relacionado con precios, estados de servicio o pagos. NO levanta la app frontend ni usa Playwright (para eso usa e2e-test-orchestrator) — esta skill es solo curl contra el backend, mucho más rápida para iterar sobre lógica de servidor.
---

# Tester de flujo de pago (curl, sin frontend)

Este proyecto quitó el pago con tarjeta (Stripe) — los únicos métodos de
pago activos son efectivo y Bizum, ambos con el mismo patrón: el cliente
"declara" el pago, y el profesional lo confirma después.

## Requisito previo

Backend local corriendo (ver hook `.claude/hooks/session-start.sh` para
tener el venv listo, o levantarlo a mano):

```bash
cd fontap-backend
rm -f /tmp/fontap_pago_test.db /tmp/uvicorn_pago_test.log
DATABASE_URL="sqlite:////tmp/fontap_pago_test.db" DESACTIVAR_RECORDATORIOS=1 \
  nohup .venv/bin/python -m uvicorn app.main:app --port 8979 > /tmp/uvicorn_pago_test.log 2>&1 &
sleep 3
```

## El script completo

```bash
API=http://127.0.0.1:8979

# 1. Registrar profesional y cliente
curl -s -X POST $API/registro -H "Content-Type: application/json" -d '{"nombre":"Pro Test","email":"protest@test.com","telefono":"600111222","password":"pass1234","tipo":"fontanero","gremio":"electricista","terminos_aceptados":true}' > /dev/null
curl -s -X POST $API/registro -H "Content-Type: application/json" -d '{"nombre":"Cliente Test","email":"clitest@test.com","telefono":"600111333","password":"pass1234","tipo":"cliente","terminos_aceptados":true}' > /dev/null

TOK_PRO=$(curl -s -X POST $API/login -H "Content-Type: application/json" -d '{"email":"protest@test.com","password":"pass1234"}' | python3 -c "import json,sys;print(json.load(sys.stdin)['access_token'])")
TOK_CLI=$(curl -s -X POST $API/login -H "Content-Type: application/json" -d '{"email":"clitest@test.com","password":"pass1234"}' | python3 -c "import json,sys;print(json.load(sys.stdin)['access_token'])")

# 2. Cliente pide el servicio directo al profesional (fontanero_id=1 en una BD nueva)
RESP=$(curl -s -X POST $API/servicios -H "Authorization: Bearer $TOK_CLI" -H "Content-Type: application/json" -d '{"tipo":"Revisión eléctrica","fontanero_id":1,"urgente":false}')
SID=$(echo "$RESP" | python3 -c "import json,sys;print(json.load(sys.stdin)['id'])")

# 3. Profesional pone precio
curl -s -X PUT $API/servicios/$SID/precio -H "Authorization: Bearer $TOK_PRO" -H "Content-Type: application/json" -d '{"precio": 45}'

# 4. Cliente acepta el precio
curl -s -X PUT $API/servicios/$SID/precio/aceptar -H "Authorization: Bearer $TOK_CLI"

# 5. Profesional marca como terminado
curl -s -X PUT $API/servicios/$SID/completar -H "Authorization: Bearer $TOK_PRO"

# 6. Cliente declara el pago (efectivo o bizum)
curl -s -X PUT $API/servicios/$SID/pagar -H "Authorization: Bearer $TOK_CLI" -H "Content-Type: application/json" -d '{"metodo":"efectivo"}'

# 7. Profesional confirma haberlo recibido
curl -s -X PUT $API/servicios/$SID/confirmar_efectivo -H "Authorization: Bearer $TOK_PRO"

# 8. Verificar estado final (debe ser "pagado")
curl -s $API/servicios/$SID -H "Authorization: Bearer $TOK_CLI" | python3 -c "import json,sys;print(json.load(sys.stdin)['estado'])"

# 9. Verificar que la comisión pendiente del profesional se registró
curl -s $API/fontaneros/1/comision-pendiente -H "Authorization: Bearer $TOK_PRO"
```

Para probar Bizum en vez de efectivo, cambiá `"metodo":"efectivo"` por
`"metodo":"bizum"` en el paso 6 y usá `/confirmar_bizum` en el paso 7.

Nota: si el profesional de prueba todavía tiene "trabajos gratis" sin
estrenar (campo `primeros_trabajos_gratis`), el paso 9 puede devolver
`total: 0` — no es un bug, es el beneficio de los primeros trabajos sin
comisión. Para probar comisión real, agotá los trabajos gratis primero o
verificá directamente el campo `comision_aplicada` del servicio en vez del
total acumulado.

## Limpieza al terminar

```bash
pkill -f "uvicorn app.main:app --port 8979" 2>/dev/null
rm -f /tmp/fontap_pago_test.db /tmp/uvicorn_pago_test.log
```

## Qué NO hacer

No pruebes con Stripe/tarjeta — ese método ya no existe en el proyecto,
cualquier endpoint `/stripe/*` debería devolver 404.
