"""
Crea 10 profesionales de demostración (uno de cada gremio, en distintas
ciudades) con descripción, servicios y precios, y varias reseñas reales
cada uno (para que la valoración y el número de trabajos salgan calculados,
no inventados). Pensado para probar visualmente ListaProfesionalesScreen
y el mapa con datos variados.

No toca ningún usuario existente: si un profesional con ese email ya
existe, se salta.

Uso:
    cd fontap-backend
    python scripts/seed_profesionales_demo.py

Si no se pasa DATABASE_URL, usa el sqlite local (./fontap.db); en Railway
ya está puesta la DATABASE_URL de producción, así que basta con correr el
comando de arriba tal cual en la Console.
"""
import base64
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import SessionLocal, engine, Base
from app import models, auth

Base.metadata.create_all(bind=engine)

CENTRO_CIUDAD = {
    "Bilbao": (43.2630, -2.9350),
    "Madrid": (40.4168, -3.7038),
    "Valencia": (39.4699, -0.3763),
    "Sevilla": (37.3891, -5.9845),
    "Barcelona": (41.3851, 2.1734),
}

PARTES = [
    "W3sibm9tYnJlIjogIk1hcmlhIEdvbnphbGV6IiwgImVtYWlsIjogIm1hcmlhLmdvbnphbGV6LmRlbW9AbXVsdGlzZXJ2aWNpb3Nwcm92ZW56YS5jb20iLCAiZ3JlbWlvIjogImxpbXBpZXphIiwgIn"
    "pvbmEiOiAiQmlsYmFvIiwgImRlc2NyaXBjaW9uIjogIkhvbGEsIHNveSBNYXJpYSB5IGxsZXZvIG1hcyBkZSAxMiBhw7FvcyBkZWRpY2FkYSBhIGxhIGxpbXBpZXphIHByb2Zlc2lvbmFsIGRlIGhv"
    "Z2FyZXMsIG9maWNpbmFzIHkgbG9jYWxlcyBjb21lcmNpYWxlcy4gTWUgZXNwZWNpYWxpem8gZW4gbGltcGllemFzIGEgZm9uZG8sIHBvc3Qtb2JyYSB5IG1hbnRlbmltaWVudG8gcGVyaW9kaWNvLi"
    "BTaWVtcHJlIHVzbyBwcm9kdWN0b3MgZGUgY2FsaWRhZCB5IGN1aWRvIGNhZGEgZGV0YWxsZSBjb21vIHNpIGZ1ZXJhIG1pIHByb3BpYSBjYXNhLiBEaXNwb25pYmxlIHRhbWJpZW4gcGFyYSB0cmFi"
    "YWpvcyBwdW50dWFsZXMgZGUgZmluIGRlIHNlbWFuYS4iLCAidmVyaWZpY2FkbyI6IHRydWUsICJjZXJ0aWZpY2Fkb19wcm8iOiB0cnVlLCAiZGlzcG9uaWJsZSI6IHRydWUsICJkaXNwb25pYmxlXz"
    "I0aCI6IGZhbHNlLCAic2VydmljaW9zIjogW1siTGltcGllemEgZ2VuZXJhbCIsIDE4XSwgWyJMaW1waWV6YSBhIGZvbmRvIiwgMzVdLCBbIkxpbXBpZXphIHBvc3Qtb2JyYSIsIDYwXV0sICJyZXNl"
    "bmFzIjogW1s1LCA1LCA0LCA1LCAiSW1wZWNhYmxlLCBtdXkgcHVudHVhbCB5IG1pbnVjaW9zYS4iXSwgWzUsIDQsIDUsIDUsICJMYSBtZWpvciBsaW1waWFkb3JhIHF1ZSBoZW1vcyB0ZW5pZG8uIl"
    "0sIFs0LCA1LCA0LCA0LCAiTXV5IGJ1ZW5hLCByZXBldGltb3Mgc2VndXJvLiJdLCBbNSwgNSwgNSwgNSwgIlBlcmZlY3RhIGVuIHRvZG8uIl1dfSwgeyJub21icmUiOiAiSm9uIEV0eGViZXJyaWEi"
    "LCAiZW1haWwiOiAiam9uLmV0eGViZXJyaWEuZGVtb0BtdWx0aXNlcnZpY2lvc3Byb3ZlbnphLmNvbSIsICJncmVtaW8iOiAiZm9udGFuZXJvIiwgInpvbmEiOiAiQmlsYmFvIiwgImRlc2NyaXBjaW"
    "9uIjogIkZvbnRhbmVybyBjb24gOCBhw7FvcyBkZSBleHBlcmllbmNpYS4gRXNwZWNpYWxpc3RhIGVuIGF2ZXJpYXMgdXJnZW50ZXMsIGluc3RhbGFjaW9uIGRlIGNhbGRlcmFzIHkgcmVmb3JtYXMg"
    "ZGUgYmHDsW8gY29tcGxldGFzLiBUcmFiYWpvIGNvbiB5IHNpbiBjaXRhLCB0YW1iaWVuIGZ1ZXJhIGRlIGhvcmFyaW8gcGFyYSB1cmdlbmNpYXMuIiwgInZlcmlmaWNhZG8iOiB0cnVlLCAiY2VydG"
    "lmaWNhZG9fcHJvIjogZmFsc2UsICJkaXNwb25pYmxlIjogdHJ1ZSwgImRpc3BvbmlibGVfMjRoIjogdHJ1ZSwgInNlcnZpY2lvcyI6IFtbIkRlc2F0YXNjbyIsIDQwXSwgWyJGdWdhIGRlIGFndWEi"
    "LCA1NV0sIFsiSW5zdGFsYWNpb24gY2FsZGVyYSIsIDE4MF1dLCAicmVzZW5hcyI6IFtbNSwgNCwgNCwgNSwgIlZpbm8gcmFwaWRvIHkgc29sdWNpb25vIGxhIGZ1Z2EgZW4gbWludXRvcy4iXSwgWz"
    "QsIDQsIDMsIDQsICJCdWVuIHRyYWJham8sIHVuIHBvY28gY2Fyby4iXSwgWzUsIDUsIDUsIDUsICJFeGNlbGVudGUsIG11eSBwcm9mZXNpb25hbC4iXV19LCB7Im5vbWJyZSI6ICJMdWNpYSBGZXJu"
    "YW5kZXoiLCAiZW1haWwiOiAibHVjaWEuZmVybmFuZGV6LmRlbW9AbXVsdGlzZXJ2aWNpb3Nwcm92ZW56YS5jb20iLCAiZ3JlbWlvIjogImVsZWN0cmljaXN0YSIsICJ6b25hIjogIk1hZHJpZCIsIC"
    "JkZXNjcmlwY2lvbiI6ICJFbGVjdHJpY2lzdGEgYXV0b25vbWEsIGJvbGV0aW5lcyBlbGVjdHJpY29zLCBjdWFkcm9zIHkgYXZlcmlhcy4iLCAidmVyaWZpY2FkbyI6IHRydWUsICJjZXJ0aWZpY2Fk"
    "b19wcm8iOiBmYWxzZSwgImRpc3BvbmlibGUiOiB0cnVlLCAiZGlzcG9uaWJsZV8yNGgiOiBmYWxzZSwgInNlcnZpY2lvcyI6IFtbIkF2ZXJpYSBlbGVjdHJpY2EiLCA0NV0sIFsiSW5zdGFsYWNpb2"
    "4gZGUgZW5jaHVmZXMiLCAyNV1dLCAicmVzZW5hcyI6IFtbNCwgNCwgNCwgNCwgIkNvcnJlY3RhIHkgcHVudHVhbC4iXSwgWzUsIDUsIDQsIDUsICJNdXkgcmVjb21lbmRhYmxlLiJdXX0sIHsibm9t"
    "YnJlIjogIkFpdG9yIE1lbmRpemFiYWwiLCAiZW1haWwiOiAiYWl0b3IubWVuZGl6YWJhbC5kZW1vQG11bHRpc2VydmljaW9zcHJvdmVuemEuY29tIiwgImdyZW1pbyI6ICJjZXJyYWplcm8iLCAiem"
    "9uYSI6ICJCaWxiYW8iLCAiZGVzY3JpcGNpb24iOiAiQ2VycmFqZXJvIGRlIHVyZ2VuY2lhcyAyNGguIEFwZXJ0dXJhIGRlIHB1ZXJ0YXMgc2luIGRlc3Ryb3pvcywgY2FtYmlvIGRlIGJvbWJpbmVz"
    "IGRlIHNlZ3VyaWRhZCB5IGluc3RhbGFjaW9uIGRlIGNlcnJhZHVyYXMgYW50aS1idW1waW5nLiBNYXMgZGUgMTUgYcOxb3MgZW4gZWwgb2ZpY2lvLCByZXNwdWVzdGEgZW4gbWVub3MgZGUgMzAgbW"
    "ludXRvcyBlbiBsYSB6b25hIGRlIEJpbGJhby4iLCAidmVyaWZpY2FkbyI6IHRydWUsICJjZXJ0aWZpY2Fkb19wcm8iOiB0cnVlLCAiZGlzcG9uaWJsZSI6IHRydWUsICJkaXNwb25pYmxlXzI0aCI6"
    "IHRydWUsICJzZXJ2aWNpb3MiOiBbWyJBcGVydHVyYSBkZSBwdWVydGEiLCA1MF0sIFsiQ2FtYmlvIGRlIGJvbWJpbiIsIDQ1XSwgWyJDZXJyYWR1cmEgZGUgc2VndXJpZGFkIiwgMTIwXV0sICJyZX"
    "NlbmFzIjogW1s1LCA1LCA1LCA1LCAiTGxlZ28gZW4gMjAgbWludXRvcywgdW4gY3JhY2suIl0sIFs1LCA0LCA0LCA1LCAiUmFwaWRvIHkgc2luIGRlc3Ryb3phciBsYSBwdWVydGEuIl0sIFs1LCA1"
    "LCA1LCA0LCAiTXV5IHByb2Zlc2lvbmFsLCBkZSBub2NoZSBhZGVtYXMuIl0sIFs0LCA0LCA0LCA0LCAiQmllbiBwZXJvIGFsZ28gY2FybyBwb3Igc2VyIGRlIG1hZHJ1Z2FkYS4iXSwgWzUsIDUsID"
    "UsIDUsICJQZXJmZWN0bywgbG8gcmVjb21pZW5kbyBtdWNoby4iXV19LCB7Im5vbWJyZSI6ICJTb2ZpYSBSYW1pcmV6IiwgImVtYWlsIjogInNvZmlhLnJhbWlyZXouZGVtb0BtdWx0aXNlcnZpY2lv"
    "c3Byb3ZlbnphLmNvbSIsICJncmVtaW8iOiAicGludG9yIiwgInpvbmEiOiAiVmFsZW5jaWEiLCAiZGVzY3JpcGNpb24iOiAiUGludG9yYSBkZWNvcmF0aXZhLCBpbnRlcmlvcmVzIHkgZXh0ZXJpb3"
    "JlcywgYWxpc2Fkb3MgeSBnb3RlbGUuIiwgInZlcmlmaWNhZG8iOiBmYWxzZSwgImNlcnRpZmljYWRvX3BybyI6IGZhbHNlLCAiZGlzcG9uaWJsZSI6IHRydWUsICJkaXNwb25pYmxlXzI0aCI6IGZh"
    "bHNlLCAic2VydmljaW9zIjogW1siUGludHVyYSBoYWJpdGFjaW9uIiwgOTBdLCBbIkFsaXNhZG8gZGUgcGFyZWRlcyIsIDE1MF1dLCAicmVzZW5hcyI6IFtdfSwgeyJub21icmUiOiAiTWlrZWwgVX"
    "JhbmdhIiwgImVtYWlsIjogIm1pa2VsLnVyYW5nYS5kZW1vQG11bHRpc2VydmljaW9zcHJvdmVuemEuY29tIiwgImdyZW1pbyI6ICJjYXJwaW50ZXJvIiwgInpvbmEiOiAiQmlsYmFvIiwgImRlc2Ny"
    "aXBjaW9uIjogIkNhcnBpbnRlcm8gYSBtZWRpZGE6IGFybWFyaW9zIGVtcG90cmFkb3MsIHB1ZXJ0YXMsIHRhcmltYSB5IG11ZWJsZXMgZGUgY29jaW5hLiBUcmFiYWpvIHRhbnRvIHJlc3RhdXJhY2"
    "lvbiBjb21vIG9icmEgbnVldmEsIHByZXN1cHVlc3RvIHNpbiBjb21wcm9taXNvLiIsICJ2ZXJpZmljYWRvIjogdHJ1ZSwgImNlcnRpZmljYWRvX3BybyI6IGZhbHNlLCAiZGlzcG9uaWJsZSI6IGZh"
    "bHNlLCAiZGlzcG9uaWJsZV8yNGgiOiBmYWxzZSwgInNlcnZpY2lvcyI6IFtbIkFybWFyaW8gYSBtZWRpZGEiLCA0MDBdLCBbIlJlcGFyYWNpb24gcHVlcnRhIiwgNjBdXSwgInJlc2VuYXMiOiBbWz"
    "UsIDUsIDQsIDUsICJVbiBhcnRlc2FubyBkZSB2ZXJkYWQsIGFjYWJhZG9zIHBlcmZlY3Rvcy4iXSwgWzQsIDUsIDUsIDQsICJNdXkgY29udGVudG9zIGNvbiBlbCBhcm1hcmlvLiJdXX0sIHsibm9t"
    "YnJlIjogIkNhcm1lbiBJYmHDsWV6IiwgImVtYWlsIjogImNhcm1lbi5pYmFuZXouZGVtb0BtdWx0aXNlcnZpY2lvc3Byb3ZlbnphLmNvbSIsICJncmVtaW8iOiAiamFyZGluZXJvIiwgInpvbmEiOi"
    "AiU2V2aWxsYSIsICJkZXNjcmlwY2lvbiI6ICJKYXJkaW5lcmEgeSBwYWlzYWppc3RhLCBtYW50ZW5pbWllbnRvIGRlIGphcmRpbmVzIHkgcG9kYXMuIiwgInZlcmlmaWNhZG8iOiB0cnVlLCAiY2Vy"
    "dGlmaWNhZG9fcHJvIjogZmFsc2UsICJkaXNwb25pYmxlIjogdHJ1ZSwgImRpc3BvbmlibGVfMjRoIjogZmFsc2UsICJzZXJ2aWNpb3MiOiBbWyJNYW50ZW5pbWllbnRvIGphcmRpbiIsIDM1XSwgWy"
    "JQb2RhIGRlIGFyYm9sZXMiLCA3MF1dLCAicmVzZW5hcyI6IFtbNCwgNCwgNCwgNSwgIkJ1ZW4gdHJhYmFqbyBjb24gZWwgamFyZGluLiJdXX0sIHsibm9tYnJlIjogIklrZXIgWnViaXphcnJldGEi"
    "LCAiZW1haWwiOiAiaWtlci56dWJpemFycmV0YS5kZW1vQG11bHRpc2VydmljaW9zcHJvdmVuemEuY29tIiwgImdyZW1pbyI6ICJjbGltYXRpemFjaW9uIiwgInpvbmEiOiAiQmlsYmFvIiwgImRlc2"
    "NyaXBjaW9uIjogIkluc3RhbGFkb3IgZGUgYWlyZSBhY29uZGljaW9uYWRvIHkgY2xpbWF0aXphY2lvbiwgbWFudGVuaW1pZW50byBkZSBjYWxkZXJhcyB5IGJvbWJhcyBkZSBjYWxvci4gU2Vydmlj"
    "aW8gdGVjbmljbyBvZmljaWFsIGRlIHZhcmlhcyBtYXJjYXMsIGNvbiBnYXJhbnRpYSBlbiB0b2RhcyBsYXMgaW5zdGFsYWNpb25lcy4iLCAidmVyaWZpY2FkbyI6IHRydWUsICJjZXJ0aWZpY2Fkb1"
    "9wcm8iOiB0cnVlLCAiZGlzcG9uaWJsZSI6IHRydWUsICJkaXNwb25pYmxlXzI0aCI6IGZhbHNlLCAic2VydmljaW9zIjogW1siSW5zdGFsYWNpb24gQS9DIiwgMjUwXSwgWyJNYW50ZW5pbWllbnRv"
    "IGFudWFsIiwgNjBdLCBbIlJldmlzaW9uIGRlIGdhcyIsIDQ1XV0sICJyZXNlbmFzIjogW1s1LCA1LCA1LCA1LCAiSW5zdGFsYWNpb24gcGVyZmVjdGEgeSBtdXkgbGltcGlhLiJdLCBbNSwgNCwgNC"
    "wgNSwgIk11eSBwcm9mZXNpb25hbCwgZXhwbGljbyB0b2RvIGJpZW4uIl0sIFs0LCA1LCA0LCA0LCAiQnVlbiBwcmVjaW8geSBidWVuIHRyYWJham8uIl1dfSwgeyJub21icmUiOiAiRWxlbmEgVG9y"
    "cmVzIiwgImVtYWlsIjogImVsZW5hLnRvcnJlcy5kZW1vQG11bHRpc2VydmljaW9zcHJvdmVuemEuY29tIiwgImdyZW1pbyI6ICJtdWRhbnphcyIsICJ6b25hIjogIkJhcmNlbG9uYSIsICJkZXNjcm"
    "lwY2lvbiI6ICJFcXVpcG8gZGUgbXVkYW56YXMgcmFwaWRvIHkgY3VpZGFkb3NvLCBwcmVzdXB1ZXN0byBjZXJyYWRvLiIsICJ2ZXJpZmljYWRvIjogZmFsc2UsICJjZXJ0aWZpY2Fkb19wcm8iOiBm"
    "YWxzZSwgImRpc3BvbmlibGUiOiB0cnVlLCAiZGlzcG9uaWJsZV8yNGgiOiBmYWxzZSwgInNlcnZpY2lvcyI6IFtbIk11ZGFuemEgcGVxdWXDsWEiLCAxNTBdLCBbIk11ZGFuemEgY29tcGxldGEiLC"
    "A0MDBdXSwgInJlc2VuYXMiOiBbWzMsIDQsIDMsIDQsICJDb3JyZWN0byBwZXJvIGxsZWdhcm9uIHRhcmRlLiJdXX0sIHsibm9tYnJlIjogIlJhdWwgSXR1cmJlIiwgImVtYWlsIjogInJhdWwuaXR1"
    "cmJlLmRlbW9AbXVsdGlzZXJ2aWNpb3Nwcm92ZW56YS5jb20iLCAiZ3JlbWlvIjogImFsYmHDsWlsIiwgInpvbmEiOiAiQmlsYmFvIiwgImRlc2NyaXBjaW9uIjogIkFsYmHDsWlsIGNvbiBtYXMgZG"
    "UgMjAgYcOxb3MgZGUgZXhwZXJpZW5jaWEgZW4gcmVmb3JtYXMgaW50ZWdyYWxlcywgYWxpY2F0YWRvcyB5IHRhYmlxdWVyaWEuIFByZXN1cHVlc3RvcyBkZXRhbGxhZG9zIHkgcGxhem9zIHF1ZSBz"
    "ZSBjdW1wbGVuLiIsICJ2ZXJpZmljYWRvIjogdHJ1ZSwgImNlcnRpZmljYWRvX3BybyI6IGZhbHNlLCAiZGlzcG9uaWJsZSI6IHRydWUsICJkaXNwb25pYmxlXzI0aCI6IGZhbHNlLCAic2VydmljaW"
    "9zIjogW1siUmVmb3JtYSBiYcOxbyIsIDEyMDBdLCBbIkFsaWNhdGFkbyIsIDMwMF0sIFsiVGFiaXF1ZSBkZSBwbGFkdXIiLCAxODBdXSwgInJlc2VuYXMiOiBbWzUsIDQsIDUsIDQsICJSZWZvcm1h"
    "IGRlbCBiYcOxbyBxdWVkbyBnZW5pYWwuIl0sIFs0LCA0LCA0LCA0LCAiQ3VtcGxpbyBsb3MgcGxhem9zLCBjb250ZW50by4iXV19XQ=="
]
DATOS_B64 = "".join(PARTES)
profesionales = json.loads(base64.b64decode(DATOS_B64))

db = SessionLocal()

cliente_demo = db.query(models.Usuario).filter(models.Usuario.email == "cliente.demo@multiserviciosprovenza.com").first()
if not cliente_demo:
    cliente_demo = models.Usuario(nombre="Cliente Demo", email="cliente.demo@multiserviciosprovenza.com",
        password_hash=auth.hashear_password(os.urandom(16).hex()), tipo="cliente",
        terminos_aceptados=True, email_verificado=True)
    db.add(cliente_demo)
    db.commit()
    db.refresh(cliente_demo)

creados = []
for idx, p in enumerate(profesionales):
    existente = db.query(models.Usuario).filter(models.Usuario.email == p["email"]).first()
    if existente:
        print("Ya existe, se salta:", p["email"])
        continue

    u = models.Usuario(nombre=p["nombre"], email=p["email"],
        password_hash=auth.hashear_password(os.urandom(16).hex()), tipo="fontanero",
        telefono="600000000", terminos_aceptados=True, email_verificado=True)
    db.add(u)
    db.commit()
    db.refresh(u)

    lat0, lon0 = CENTRO_CIUDAD.get(p["zona"], CENTRO_CIUDAD["Bilbao"])
    # pequeño desplazamiento para que no queden todos apilados en el mismo punto
    lat = lat0 + (idx % 5) * 0.006 - 0.012
    lon = lon0 + (idx % 3) * 0.006 - 0.006

    f = models.Fontanero(usuario_id=u.id, nombre=p["nombre"], gremio=p["gremio"], zona=p["zona"],
        descripcion=p["descripcion"], verificado=p["verificado"], certificado_pro=p["certificado_pro"],
        disponible=p["disponible"], disponible_24h=p["disponible_24h"],
        latitud=lat, longitud=lon)
    db.add(f)
    db.commit()
    db.refresh(f)

    for nombre_serv, precio in p["servicios"]:
        db.add(models.ServicioFontanero(fontanero_id=f.id, nombre=nombre_serv, precio=precio, activo=True))
    db.commit()

    for (pu, ca, pj, tr, com) in p["resenas"]:
        serv = models.Servicio(cliente_id=cliente_demo.id, fontanero_id=f.id, tipo=p["gremio"],
            estado="pagado", gremio=p["gremio"])
        db.add(serv)
        db.commit()
        db.refresh(serv)
        db.add(models.Resena(servicio_id=serv.id, cliente_id=cliente_demo.id, fontanero_id=f.id,
            puntualidad=pu, calidad=ca, precio_justo=pj, trato=tr, comentario=com))
        db.commit()

    if p["resenas"]:
        resenas_f = db.query(models.Resena).filter(models.Resena.fontanero_id == f.id).all()
        total = sum((r.puntualidad + r.calidad + r.precio_justo + r.trato) / 4 for r in resenas_f)
        f.valoracion = round(total / len(resenas_f), 2)
        f.num_trabajos = len(resenas_f)
        db.commit()

    creados.append(p["nombre"])

print("Creados:", len(creados))
for n in creados:
    print(" -", n)
