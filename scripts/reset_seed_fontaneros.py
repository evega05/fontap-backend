"""
Borra todos los clientes y fontaneros (y todo lo que depende de ellos:
servicios, mensajes, ofertas, reseñas, citas, favoritos, etc.) y crea
10 fontaneros de prueba en distintas zonas de Bilbao, cada uno con
2-3 servicios y precios.

Los usuarios admin / administrador_fincas y sus datos NO se tocan.

Uso:
    cd fontap-backend
    DATABASE_URL="postgresql://..."  python scripts/reset_seed_fontaneros.py

Si no se pasa DATABASE_URL, usa el sqlite local (./fontap.db), igual
que la app.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import SessionLocal, engine, Base
from app import models, auth

Base.metadata.create_all(bind=engine)

PASSWORD_PRUEBA = "fontanero123"

FONTANEROS = [
    dict(nombre="Marta Fontanera", zona="Bilbao Centro", lat=43.2630, lon=-2.9350,
         disponible=True, disponible_24h=True, valoracion=5.0, verificado=True, num_trabajos=42,
         servicios=[("Desatasco de tuberías", 45), ("Reparación de fugas", 60)]),
    dict(nombre="Carlos Ruiz", zona="Deusto", lat=43.2744, lon=-2.9494,
         disponible=True, disponible_24h=False, valoracion=4.7, verificado=True, num_trabajos=31,
         servicios=[("Reparación de fugas", 55), ("Instalación de grifos", 35)]),
    dict(nombre="Laura Gómez", zona="Indautxu", lat=43.2607, lon=-2.9423,
         disponible=True, disponible_24h=False, valoracion=4.9, verificado=True, num_trabajos=58,
         servicios=[("Revisión de caldera", 50), ("Cambio de radiador", 80)]),
    dict(nombre="Javier Torres", zona="Abando", lat=43.2601, lon=-2.9252,
         disponible=False, disponible_24h=False, valoracion=4.4, verificado=False, num_trabajos=12,
         servicios=[("Desatasco de tuberías", 40), ("Revisión de caldera", 55)]),
    dict(nombre="Sofía Martín", zona="Begoña", lat=43.2669, lon=-2.9280,
         disponible=True, disponible_24h=True, valoracion=4.6, verificado=True, num_trabajos=27,
         servicios=[("Reparación de fugas", 60), ("Instalación de grifos", 38)]),
    dict(nombre="Pedro Sánchez", zona="Rekalde", lat=43.2528, lon=-2.9450,
         disponible=True, disponible_24h=False, valoracion=4.2, verificado=False, num_trabajos=9,
         servicios=[("Desatasco de tuberías", 42), ("Cambio de radiador", 75)]),
    dict(nombre="Ana López", zona="Basurto", lat=43.2618, lon=-2.9560,
         disponible=True, disponible_24h=False, valoracion=4.8, verificado=True, num_trabajos=64,
         servicios=[("Revisión de caldera", 52), ("Reparación de fugas", 58)]),
    dict(nombre="Diego Fernández", zona="Santutxu", lat=43.2570, lon=-2.9130,
         disponible=False, disponible_24h=False, valoracion=4.5, verificado=True, num_trabajos=19,
         servicios=[("Instalación de grifos", 36), ("Desatasco de tuberías", 44)]),
    dict(nombre="Elena Castro", zona="Zorroza", lat=43.2820, lon=-2.9750,
         disponible=True, disponible_24h=True, valoracion=4.3, verificado=False, num_trabajos=15,
         servicios=[("Cambio de radiador", 78), ("Revisión de caldera", 48)]),
    dict(nombre="Miguel Ángel Ibáñez", zona="San Ignacio", lat=43.2685, lon=-2.9610,
         disponible=True, disponible_24h=False, valoracion=5.0, verificado=True, num_trabajos=71,
         servicios=[("Reparación de fugas", 62), ("Desatasco de tuberías", 46)]),
]


def reset_datos(db):
    ids_borrados = [
        u.id for u in db.query(models.Usuario.id)
        .filter(models.Usuario.tipo.in_(["cliente", "fontanero"]))
    ]

    db.query(models.Mensaje).delete(synchronize_session=False)
    db.query(models.ImagenServicio).delete(synchronize_session=False)
    db.query(models.Oferta).delete(synchronize_session=False)
    db.query(models.Resena).delete(synchronize_session=False)
    db.query(models.Cita).delete(synchronize_session=False)
    db.query(models.Favorito).delete(synchronize_session=False)
    db.query(models.Notificacion).filter(
        models.Notificacion.usuario_id.in_(ids_borrados)
    ).delete(synchronize_session=False)
    db.query(models.TokenPush).filter(
        models.TokenPush.usuario_id.in_(ids_borrados)
    ).delete(synchronize_session=False)
    db.query(models.DocumentoVerificacion).delete(synchronize_session=False)
    db.query(models.GaleriaFontanero).delete(synchronize_session=False)
    db.query(models.BloqueoHorario).delete(synchronize_session=False)
    db.query(models.HorarioBase).delete(synchronize_session=False)
    db.query(models.ServicioFontanero).delete(synchronize_session=False)
    db.query(models.Servicio).delete(synchronize_session=False)
    db.query(models.Fontanero).delete(synchronize_session=False)
    db.query(models.Usuario).filter(
        models.Usuario.tipo.in_(["cliente", "fontanero"])
    ).delete(synchronize_session=False)
    db.commit()
    print(f"Borrados {len(ids_borrados)} usuarios (clientes/fontaneros) y sus datos asociados.")


def sembrar_fontaneros(db):
    hash_pw = auth.hashear_password(PASSWORD_PRUEBA)
    for i, f in enumerate(FONTANEROS, start=1):
        email = f"fontanero{i}@fontap.test"
        usuario = models.Usuario(
            nombre=f["nombre"], email=email, telefono=f"6000000{i:02d}",
            password_hash=hash_pw, tipo="fontanero",
        )
        db.add(usuario)
        db.flush()

        fontanero = models.Fontanero(
            usuario_id=usuario.id, nombre=f["nombre"], telefono=usuario.telefono,
            zona=f["zona"], disponible=f["disponible"], disponible_24h=f["disponible_24h"],
            valoracion=f["valoracion"], latitud=f["lat"], longitud=f["lon"],
            gremio="fontanero", verificado=f["verificado"], num_trabajos=f["num_trabajos"],
            descripcion=f"Fontanero profesional en {f['zona']}, Bilbao.",
        )
        db.add(fontanero)
        db.flush()

        for nombre_serv, precio in f["servicios"]:
            db.add(models.ServicioFontanero(
                fontanero_id=fontanero.id, nombre=nombre_serv, precio=precio,
                duracion_minutos=60, activo=True,
            ))

    db.commit()
    print(f"Creados {len(FONTANEROS)} fontaneros de prueba (contraseña: '{PASSWORD_PRUEBA}').")


if __name__ == "__main__":
    db = SessionLocal()
    try:
        reset_datos(db)
        sembrar_fontaneros(db)
    finally:
        db.close()
