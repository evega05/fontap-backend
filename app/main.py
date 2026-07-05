from fastapi import FastAPI, Depends, HTTPException, status, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
from sqlalchemy import or_
from typing import List, Optional
from pydantic import BaseModel
from . import models, schemas, auth
from .database import engine, get_db
import os, uuid, requests as _http

UPLOAD_DIR = "uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)

FCM_SERVER_KEY = os.getenv("FCM_SERVER_KEY", "")

models.Base.metadata.create_all(bind=engine)

app = FastAPI(title="FonTap API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")

# ─── HELPERS ───────────────────────────────────────────────────────────────────

def get_or_create_fontanero(db: Session, usuario_id: int):
    fontanero = db.query(models.Fontanero).filter(
        models.Fontanero.usuario_id == usuario_id
    ).first()
    if fontanero:
        return fontanero
    usuario_obj = db.query(models.Usuario).filter(models.Usuario.id == usuario_id).first()
    if usuario_obj and usuario_obj.tipo == "fontanero":
        fontanero = models.Fontanero(
            usuario_id=usuario_id,
            nombre=usuario_obj.nombre,
            telefono=usuario_obj.telefono,
            disponible=True,
            disponible_24h=False,
            valoracion=5.0,
            zona="Bilbao",
        )
        db.add(fontanero)
        db.commit()
        db.refresh(fontanero)
        return fontanero
    return None

def _crear_notificacion(db: Session, usuario_id: int, titulo: str, cuerpo: str, tipo: str = None, referencia_id: int = None):
    notif = models.Notificacion(
        usuario_id=usuario_id,
        titulo=titulo,
        cuerpo=cuerpo,
        tipo=tipo,
        referencia_id=referencia_id,
    )
    db.add(notif)
    db.flush()
    if FCM_SERVER_KEY:
        tokens = db.query(models.TokenPush).filter(
            models.TokenPush.usuario_id == usuario_id,
            models.TokenPush.activo == True,
        ).all()
        for t in tokens:
            try:
                _http.post(
                    "https://fcm.googleapis.com/fcm/send",
                    json={"to": t.token, "notification": {"title": titulo, "body": cuerpo}},
                    headers={"Authorization": f"key={FCM_SERVER_KEY}"},
                    timeout=5,
                )
            except Exception:
                pass

# ─── AUTH ──────────────────────────────────────────────────────────────────────

@app.get("/")
def inicio():
    return {"mensaje": "FonTap API funcionando"}

@app.post("/migrar-db")
def migrar_db():
    from .database import engine
    from sqlalchemy import text
    columnas = [
        ("fontaneros", "descripcion", "TEXT"),
        ("fontaneros", "especialidades", "TEXT"),
        ("fontaneros", "vacaciones_desde", "TIMESTAMP"),
        ("fontaneros", "vacaciones_hasta", "TIMESTAMP"),
        ("fontaneros", "gremio", "VARCHAR DEFAULT 'fontanero'"),
        ("fontaneros", "verificado", "BOOLEAN DEFAULT FALSE"),
        ("fontaneros", "num_trabajos", "INTEGER DEFAULT 0"),
        ("fontaneros", "disponible_24h", "BOOLEAN DEFAULT FALSE"),
        ("fontaneros", "foto_url", "VARCHAR"),
        ("servicios", "urgencia_ia", "VARCHAR"),
        ("servicios", "eta_minutos", "INTEGER"),
        ("servicios", "comision_aplicada", "FLOAT"),
        ("servicios", "stripe_payment_intent", "VARCHAR"),
    ]
    resultados = []
    with engine.connect() as conn:
        for tabla, columna, tipo in columnas:
            try:
                conn.execute(text(f"ALTER TABLE {tabla} ADD COLUMN IF NOT EXISTS {columna} {tipo}"))
                conn.commit()
                resultados.append(f"OK: {tabla}.{columna}")
            except Exception as ex:
                resultados.append(f"SKIP: {tabla}.{columna} ({ex})")
    return {"resultado": resultados}

@app.post("/registro", response_model=schemas.Token)
def registrar_usuario(usuario: schemas.UsuarioRegistro, db: Session = Depends(get_db)):
    existe = db.query(models.Usuario).filter(models.Usuario.email == usuario.email).first()
    if existe:
        raise HTTPException(status_code=400, detail="Email ya registrado")
    nuevo = models.Usuario(
        nombre=usuario.nombre,
        email=usuario.email,
        telefono=usuario.telefono,
        password_hash=auth.hashear_password(usuario.password),
        tipo=usuario.tipo,
    )
    db.add(nuevo)
    db.commit()
    db.refresh(nuevo)

    if usuario.tipo == "fontanero":
        get_or_create_fontanero(db, nuevo.id)

    token = auth.crear_token({"sub": nuevo.email, "tipo": nuevo.tipo, "id": nuevo.id})
    return {
        "access_token": token,
        "token_type": "bearer",
        "tipo_usuario": nuevo.tipo,
        "nombre": nuevo.nombre,
        "id": nuevo.id,
        "email": nuevo.email,
    }

@app.post("/login", response_model=schemas.Token)
def login(datos: schemas.UsuarioLogin, db: Session = Depends(get_db)):
    usuario = db.query(models.Usuario).filter(models.Usuario.email == datos.email).first()
    if not usuario or not auth.verificar_password(datos.password, usuario.password_hash):
        raise HTTPException(status_code=401, detail="Credenciales incorrectas")
    token = auth.crear_token({"sub": usuario.email, "tipo": usuario.tipo, "id": usuario.id})
    return {
        "access_token": token,
        "token_type": "bearer",
        "tipo_usuario": usuario.tipo,
        "nombre": usuario.nombre,
        "id": usuario.id,
        "email": usuario.email,
    }

# ─── FONTANEROS ────────────────────────────────────────────────────────────────

@app.get("/fontaneros", response_model=List[schemas.FontaneroRespuesta])
def listar_fontaneros(db: Session = Depends(get_db)):
    return db.query(models.Fontanero).filter(models.Fontanero.disponible == True).all()

class DisponibilidadUpdate(BaseModel):
    disponible: bool
    disponible_24h: Optional[bool] = None

@app.put("/fontaneros/{fontanero_id}/disponibilidad")
def actualizar_disponibilidad(
    fontanero_id: int,
    datos: DisponibilidadUpdate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    fontanero = db.query(models.Fontanero).filter(
        models.Fontanero.usuario_id == fontanero_id
    ).first()
    if not fontanero:
        raise HTTPException(status_code=404, detail="Fontanero no encontrado")
    fontanero.disponible = datos.disponible
    if datos.disponible_24h is not None:
        fontanero.disponible_24h = datos.disponible_24h
    db.commit()
    return {"mensaje": "Disponibilidad actualizada"}

@app.get("/fontaneros/{fontanero_id}/solicitudes")
def ver_solicitudes_fontanero(
    fontanero_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    fontanero = get_or_create_fontanero(db, fontanero_id)
    if not fontanero:
        raise HTTPException(status_code=404, detail="Fontanero no encontrado")

    pendientes = db.query(models.Servicio).filter(
        models.Servicio.estado == "pendiente",
        or_(
            models.Servicio.fontanero_id == None,
            models.Servicio.fontanero_id == fontanero.id,
        ),
    ).all()

    propias = db.query(models.Servicio).filter(
        models.Servicio.fontanero_id == fontanero.id,
        models.Servicio.estado.in_(["aceptado", "precio_enviado", "pago_pendiente", "pagado", "completado"]),
    ).all()

    resultado = []
    for s in pendientes + propias:
        cliente = db.query(models.Usuario).filter(models.Usuario.id == s.cliente_id).first()
        resultado.append({
            "id": s.id,
            "tipo": s.tipo,
            "descripcion": s.descripcion,
            "urgente": s.urgente,
            "estado": s.estado,
            "estado_color": schemas.ESTADO_COLORES.get(s.estado, "#D97706"),
            "precio": s.precio,
            "fecha": str(s.fecha) if s.fecha else None,
            "cliente_nombre": cliente.nombre if cliente else "Cliente",
            "zona": "Bilbao",
        })
    return resultado

# ─── SERVICIOS ─────────────────────────────────────────────────────────────────

@app.post("/servicios", response_model=schemas.ServicioRespuesta)
def crear_servicio(
    servicio: schemas.ServicioCrear,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    cliente_id = current_user["id"]
    nuevo = models.Servicio(
        cliente_id=cliente_id,
        fontanero_id=servicio.fontanero_id,
        tipo=servicio.tipo,
        descripcion=servicio.descripcion,
        urgente=servicio.urgente,
        fecha=servicio.fecha,
        estado="pendiente",
        precio=None,
    )
    db.add(nuevo)
    db.commit()
    db.refresh(nuevo)
    if servicio.fontanero_id:
        fontanero_directo = db.query(models.Fontanero).filter(
            models.Fontanero.id == servicio.fontanero_id
        ).first()
        if fontanero_directo:
            _crear_notificacion(db, fontanero_directo.usuario_id, "Nueva solicitud", f"Tienes una nueva solicitud de {servicio.tipo}", "solicitud_directa", nuevo.id)
            db.commit()
    elif servicio.urgente:
        fontaneros_zona = db.query(models.Fontanero).filter(
            models.Fontanero.disponible == True,
            models.Fontanero.usuario_id != None,
        ).all()
        for f in fontaneros_zona:
            _crear_notificacion(db, f.usuario_id, "Nueva solicitud urgente", f"Solicitud urgente de {servicio.tipo} cerca de tu zona", "solicitud_urgente", nuevo.id)
        db.commit()
    return schemas.ServicioRespuesta.from_orm_with_color(nuevo)

@app.get("/servicios/abiertos", response_model=List[schemas.ServicioRespuesta])
def listar_servicios_abiertos(
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    servicios = db.query(models.Servicio).filter(
        models.Servicio.estado == "pendiente",
        models.Servicio.fontanero_id == None,
    ).order_by(models.Servicio.id.desc()).all()
    return [schemas.ServicioRespuesta.from_orm_with_color(s) for s in servicios]

@app.get("/servicios/{servicio_id}")
def ver_servicio(
    servicio_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    servicio = db.query(models.Servicio).filter(models.Servicio.id == servicio_id).first()
    if not servicio:
        raise HTTPException(status_code=404, detail="Servicio no encontrado")
    return schemas.ServicioRespuesta.from_orm_with_color(servicio)

@app.put("/servicios/{servicio_id}/aceptar")
def aceptar_servicio(
    servicio_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    fontanero_usuario_id = current_user["id"]
    servicio = db.query(models.Servicio).filter(models.Servicio.id == servicio_id).first()
    if not servicio:
        raise HTTPException(status_code=404, detail="Servicio no encontrado")
    fontanero = db.query(models.Fontanero).filter(
        models.Fontanero.usuario_id == fontanero_usuario_id
    ).first()
    if not fontanero:
        raise HTTPException(status_code=404, detail="Fontanero no encontrado")
    if servicio.fontanero_id is not None and servicio.fontanero_id != fontanero.id:
        raise HTTPException(status_code=400, detail="Este servicio ya fue asignado a otro profesional")
    servicio.fontanero_id = fontanero.id
    servicio.estado = "aceptado"
    _crear_notificacion(db, servicio.cliente_id, "Servicio aceptado", f"Un fontanero ha aceptado tu solicitud de {servicio.tipo}", "servicio_aceptado", servicio.id)
    db.commit()
    return {"mensaje": "Servicio aceptado"}

@app.put("/servicios/{servicio_id}/rechazar")
def rechazar_servicio(
    servicio_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    servicio = db.query(models.Servicio).filter(models.Servicio.id == servicio_id).first()
    if not servicio:
        raise HTTPException(status_code=404, detail="Servicio no encontrado")
    fontanero = db.query(models.Fontanero).filter(
        models.Fontanero.usuario_id == current_user["id"]
    ).first()
    if not fontanero or servicio.fontanero_id not in (None, fontanero.id):
        raise HTTPException(status_code=403, detail="No puedes rechazar un servicio que no es tuyo")
    servicio.estado = "rechazado"
    _crear_notificacion(db, servicio.cliente_id, "Servicio rechazado", f"Tu solicitud de {servicio.tipo} no pudo ser atendida", "servicio_rechazado", servicio.id)
    db.commit()
    return {"mensaje": "Servicio rechazado"}

class PrecioUpdate(BaseModel):
    precio: float

@app.put("/servicios/{servicio_id}/precio")
def enviar_precio(
    servicio_id: int,
    datos: PrecioUpdate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    servicio = db.query(models.Servicio).filter(models.Servicio.id == servicio_id).first()
    if not servicio:
        raise HTTPException(status_code=404, detail="Servicio no encontrado")
    fontanero = db.query(models.Fontanero).filter(
        models.Fontanero.usuario_id == current_user["id"]
    ).first()
    if not fontanero or servicio.fontanero_id != fontanero.id:
        raise HTTPException(status_code=403, detail="No puedes enviar precio para un servicio que no es tuyo")
    servicio.precio = datos.precio
    servicio.estado = "precio_enviado"
    _crear_notificacion(db, servicio.cliente_id, "Precio recibido", f"El fontanero ha enviado un presupuesto de {datos.precio}€", "precio_enviado", servicio.id)
    db.commit()
    return {"mensaje": "Precio enviado al cliente", "precio": datos.precio}

class PagoUpdate(BaseModel):
    metodo: str

@app.put("/servicios/{servicio_id}/pagar")
def confirmar_pago(
    servicio_id: int,
    datos: PagoUpdate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    servicio = db.query(models.Servicio).filter(models.Servicio.id == servicio_id).first()
    if not servicio:
        raise HTTPException(status_code=404, detail="Servicio no encontrado")
    if servicio.cliente_id != current_user["id"]:
        raise HTTPException(status_code=403, detail="Solo el cliente puede confirmar el pago de este servicio")
    if not servicio.precio:
        raise HTTPException(status_code=400, detail="El fontanero aún no ha enviado el precio")
    nuevo_estado = "pago_pendiente" if datos.metodo == "efectivo" else "pagado"
    servicio.estado = nuevo_estado
    servicio.metodo_pago = datos.metodo
    if servicio.fontanero_id:
        fontanero_obj = db.query(models.Fontanero).filter(models.Fontanero.id == servicio.fontanero_id).first()
        if fontanero_obj and fontanero_obj.usuario_id:
            _crear_notificacion(db, fontanero_obj.usuario_id, "Pago recibido", f"El cliente ha confirmado el pago de {servicio.precio}€ via {datos.metodo}", "pago_recibido", servicio.id)
    db.commit()
    return {"mensaje": "Pago registrado", "precio": servicio.precio, "metodo": datos.metodo}

@app.put("/servicios/{servicio_id}/confirmar_efectivo")
def confirmar_efectivo(
    servicio_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    servicio = db.query(models.Servicio).filter(models.Servicio.id == servicio_id).first()
    if not servicio:
        raise HTTPException(status_code=404, detail="Servicio no encontrado")
    servicio.estado = "pagado"
    db.commit()
    return {"mensaje": "Efectivo confirmado"}

@app.get("/clientes/{cliente_id}/servicios")
def ver_servicios_cliente(
    cliente_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    servicios = db.query(models.Servicio).filter(
        models.Servicio.cliente_id == cliente_id
    ).order_by(models.Servicio.id.desc()).all()
    resultado = []
    for s in servicios:
        data = schemas.ServicioRespuesta.from_orm_with_color(s)
        if s.fontanero_id:
            fontanero = db.query(models.Fontanero).filter(models.Fontanero.id == s.fontanero_id).first()
            data.fontanero_nombre = fontanero.nombre if fontanero else None
        if s.estado == "pendiente":
            data.num_ofertas = db.query(models.Oferta).filter(
                models.Oferta.servicio_id == s.id,
                models.Oferta.estado == "pendiente",
            ).count()
        resultado.append(data)
    return resultado

# ─── HORARIOS / BLOQUEOS / SERVICIOS FONTANERO ────────────────────────────────

def _resolver_fontanero(db: Session, fontanero_id: int) -> models.Fontanero:
    fontanero = db.query(models.Fontanero).filter(
        models.Fontanero.usuario_id == fontanero_id
    ).first()
    if not fontanero:
        raise HTTPException(status_code=404, detail="Fontanero no encontrado")
    return fontanero

@app.post("/fontaneros/{fontanero_id}/horario", response_model=schemas.HorarioBaseRespuesta)
def crear_horario(
    fontanero_id: int,
    horario: schemas.HorarioBaseCrear,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    fontanero = _resolver_fontanero(db, fontanero_id)
    nuevo = models.HorarioBase(
        fontanero_id=fontanero.id,
        dia_semana=horario.dia_semana,
        hora_inicio=horario.hora_inicio,
        hora_fin=horario.hora_fin,
        intervalo_minutos=horario.intervalo_minutos,
    )
    db.add(nuevo)
    db.commit()
    db.refresh(nuevo)
    return nuevo

@app.get("/fontaneros/{fontanero_id}/horario", response_model=List[schemas.HorarioBaseRespuesta])
def ver_horario(fontanero_id: int, db: Session = Depends(get_db)):
    fontanero = _resolver_fontanero(db, fontanero_id)
    return db.query(models.HorarioBase).filter(
        models.HorarioBase.fontanero_id == fontanero.id
    ).all()

@app.post("/fontaneros/{fontanero_id}/bloqueos", response_model=schemas.BloqueoRespuesta)
def crear_bloqueo(
    fontanero_id: int,
    bloqueo: schemas.BloqueoCrear,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    fontanero = _resolver_fontanero(db, fontanero_id)
    nuevo = models.BloqueoHorario(
        fontanero_id=fontanero.id,
        fecha=bloqueo.fecha,
        hora_inicio=bloqueo.hora_inicio,
        hora_fin=bloqueo.hora_fin,
        motivo=bloqueo.motivo,
    )
    db.add(nuevo)
    db.commit()
    db.refresh(nuevo)
    return nuevo

@app.get("/fontaneros/{fontanero_id}/bloqueos", response_model=List[schemas.BloqueoRespuesta])
def ver_bloqueos(
    fontanero_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    fontanero = _resolver_fontanero(db, fontanero_id)
    return db.query(models.BloqueoHorario).filter(
        models.BloqueoHorario.fontanero_id == fontanero.id
    ).all()

@app.delete("/fontaneros/{fontanero_id}/bloqueos/{bloqueo_id}")
def eliminar_bloqueo(
    fontanero_id: int,
    bloqueo_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    fontanero = _resolver_fontanero(db, fontanero_id)
    bloqueo = db.query(models.BloqueoHorario).filter(
        models.BloqueoHorario.id == bloqueo_id,
        models.BloqueoHorario.fontanero_id == fontanero.id,
    ).first()
    if not bloqueo:
        raise HTTPException(status_code=404, detail="Bloqueo no encontrado")
    db.delete(bloqueo)
    db.commit()
    return {"mensaje": "Bloqueo eliminado"}

@app.post("/fontaneros/{fontanero_id}/servicios", response_model=schemas.ServicioFontaneroRespuesta)
def añadir_servicio(
    fontanero_id: int,
    servicio: schemas.ServicioFontaneroCrear,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    fontanero = _resolver_fontanero(db, fontanero_id)
    nuevo = models.ServicioFontanero(
        fontanero_id=fontanero.id,
        nombre=servicio.nombre,
        precio=servicio.precio,
        duracion_minutos=servicio.duracion_minutos,
    )
    db.add(nuevo)
    db.commit()
    db.refresh(nuevo)
    return nuevo

@app.get("/fontaneros/{fontanero_id}/servicios", response_model=List[schemas.ServicioFontaneroRespuesta])
def ver_servicios_fontanero(fontanero_id: int, db: Session = Depends(get_db)):
    fontanero = _resolver_fontanero(db, fontanero_id)
    return db.query(models.ServicioFontanero).filter(
        models.ServicioFontanero.fontanero_id == fontanero.id,
        models.ServicioFontanero.activo == True,
    ).all()

@app.delete("/fontaneros/{fontanero_id}/servicios/{servicio_id}")
def eliminar_servicio(
    fontanero_id: int,
    servicio_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    fontanero = _resolver_fontanero(db, fontanero_id)
    servicio = db.query(models.ServicioFontanero).filter(
        models.ServicioFontanero.id == servicio_id,
        models.ServicioFontanero.fontanero_id == fontanero.id,
    ).first()
    if not servicio:
        raise HTTPException(status_code=404, detail="Servicio no encontrado")
    servicio.activo = False
    db.commit()
    return {"mensaje": "Servicio eliminado"}

# ─── IMÁGENES DE SERVICIO ──────────────────────────────────────────────────────

ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}

@app.post("/servicios/{servicio_id}/imagenes", response_model=schemas.ImagenRespuesta)
def subir_imagen_servicio(
    servicio_id: int,
    archivo: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    servicio = db.query(models.Servicio).filter(models.Servicio.id == servicio_id).first()
    if not servicio:
        raise HTTPException(status_code=404, detail="Servicio no encontrado")
    if archivo.content_type not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(status_code=400, detail="Tipo de archivo no permitido")
    ext = archivo.filename.rsplit(".", 1)[-1] if "." in archivo.filename else "jpg"
    nombre_archivo = f"{uuid.uuid4().hex}.{ext}"
    ruta = os.path.join(UPLOAD_DIR, nombre_archivo)
    with open(ruta, "wb") as f:
        f.write(archivo.file.read())
    imagen = models.ImagenServicio(servicio_id=servicio_id, url=f"/uploads/{nombre_archivo}")
    db.add(imagen)
    db.commit()
    db.refresh(imagen)
    return imagen

@app.get("/servicios/{servicio_id}/imagenes", response_model=List[schemas.ImagenRespuesta])
def listar_imagenes_servicio(
    servicio_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    return db.query(models.ImagenServicio).filter(
        models.ImagenServicio.servicio_id == servicio_id
    ).all()

# ─── CHAT ──────────────────────────────────────────────────────────────────────

@app.put("/servicios/{servicio_id}/mensajes/leer")
def marcar_mensajes_leidos(
    servicio_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    db.query(models.Mensaje).filter(
        models.Mensaje.servicio_id == servicio_id,
        models.Mensaje.emisor_id != current_user["id"],
        models.Mensaje.leido == False,
    ).update({"leido": True})
    db.commit()
    return {"mensaje": "Mensajes marcados como leídos"}

@app.get("/usuarios/{usuario_id}/chats")
def listar_chats_recientes(
    usuario_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    fontanero = db.query(models.Fontanero).filter(models.Fontanero.usuario_id == usuario_id).first()
    fontanero_id = fontanero.id if fontanero else None
    servicios = db.query(models.Servicio).filter(
        or_(
            models.Servicio.cliente_id == usuario_id,
            models.Servicio.fontanero_id == fontanero_id,
        )
    ).all()

    resultado = []
    for s in servicios:
        ultimo = db.query(models.Mensaje).filter(
            models.Mensaje.servicio_id == s.id
        ).order_by(models.Mensaje.creado_en.desc()).first()
        if not ultimo:
            continue
        no_leidos = db.query(models.Mensaje).filter(
            models.Mensaje.servicio_id == s.id,
            models.Mensaje.emisor_id != usuario_id,
            models.Mensaje.leido == False,
        ).count()
        if usuario_id == s.cliente_id:
            otro_nombre = "Profesional"
            if s.fontanero_id:
                fontanero_obj = db.query(models.Fontanero).filter(models.Fontanero.id == s.fontanero_id).first()
                if fontanero_obj and fontanero_obj.nombre:
                    otro_nombre = fontanero_obj.nombre
        else:
            cliente_obj = db.query(models.Usuario).filter(models.Usuario.id == s.cliente_id).first()
            otro_nombre = cliente_obj.nombre if cliente_obj else "Cliente"
        resultado.append({
            "servicio_id": s.id,
            "tipo_servicio": s.tipo,
            "estado": s.estado,
            "otro_participante": otro_nombre,
            "ultimo_mensaje": ultimo.texto,
            "ultimo_mensaje_fecha": str(ultimo.creado_en),
            "no_leidos": no_leidos,
        })
    resultado.sort(key=lambda c: c["ultimo_mensaje_fecha"], reverse=True)
    return resultado

# ─── NOTIFICACIONES PUSH ───────────────────────────────────────────────────────

@app.post("/usuarios/{usuario_id}/push-token")
def registrar_push_token(
    usuario_id: int,
    datos: schemas.TokenPushCrear,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    existente = db.query(models.TokenPush).filter(models.TokenPush.token == datos.token).first()
    if existente:
        existente.usuario_id = usuario_id
        existente.activo = True
        existente.plataforma = datos.plataforma
    else:
        db.add(models.TokenPush(usuario_id=usuario_id, token=datos.token, plataforma=datos.plataforma))
    db.commit()
    return {"mensaje": "Token registrado"}

@app.delete("/usuarios/{usuario_id}/push-token")
def eliminar_push_token(
    usuario_id: int,
    datos: schemas.TokenPushCrear,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    db.query(models.TokenPush).filter(
        models.TokenPush.usuario_id == usuario_id,
        models.TokenPush.token == datos.token,
    ).update({"activo": False})
    db.commit()
    return {"mensaje": "Token eliminado"}

@app.get("/usuarios/{usuario_id}/notificaciones", response_model=List[schemas.NotificacionRespuesta])
def listar_notificaciones(
    usuario_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    return db.query(models.Notificacion).filter(
        models.Notificacion.usuario_id == usuario_id
    ).order_by(models.Notificacion.creado_en.desc()).limit(50).all()

@app.put("/notificaciones/{notif_id}/leer")
def marcar_notificacion_leida(
    notif_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    notif = db.query(models.Notificacion).filter(
        models.Notificacion.id == notif_id,
        models.Notificacion.usuario_id == current_user["id"],
    ).first()
    if not notif:
        raise HTTPException(status_code=404, detail="Notificación no encontrada")
    notif.leida = True
    db.commit()
    return {"mensaje": "Notificación marcada como leída"}

@app.put("/usuarios/{usuario_id}/notificaciones/leer-todas")
def marcar_todas_leidas(
    usuario_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    db.query(models.Notificacion).filter(
        models.Notificacion.usuario_id == usuario_id,
        models.Notificacion.leida == False,
    ).update({"leida": True})
    db.commit()
    return {"mensaje": "Todas las notificaciones marcadas como leídas"}

# ─── PERFIL FONTANERO ──────────────────────────────────────────────────────────

@app.put("/fontaneros/{fontanero_id}/perfil")
def actualizar_perfil_fontanero(
    fontanero_id: int,
    datos: schemas.FontaneroActualizar,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    fontanero = db.query(models.Fontanero).filter(
        models.Fontanero.usuario_id == fontanero_id
    ).first()
    if not fontanero:
        raise HTTPException(status_code=404, detail="Fontanero no encontrado")
    if datos.zona is not None:
        fontanero.zona = datos.zona
    if datos.descripcion is not None:
        fontanero.descripcion = datos.descripcion
    if datos.especialidades is not None:
        fontanero.especialidades = datos.especialidades
    if datos.disponible_24h is not None:
        fontanero.disponible_24h = datos.disponible_24h
    db.commit()
    return {"mensaje": "Perfil actualizado"}

@app.post("/fontaneros/{fontanero_id}/foto")
def subir_foto_perfil(
    fontanero_id: int,
    archivo: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    fontanero = db.query(models.Fontanero).filter(
        models.Fontanero.usuario_id == fontanero_id
    ).first()
    if not fontanero:
        raise HTTPException(status_code=404, detail="Fontanero no encontrado")
    if archivo.content_type not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(status_code=400, detail="Tipo de archivo no permitido")
    ext = archivo.filename.rsplit(".", 1)[-1] if "." in archivo.filename else "jpg"
    nombre_archivo = f"perfil_{uuid.uuid4().hex}.{ext}"
    ruta = os.path.join(UPLOAD_DIR, nombre_archivo)
    with open(ruta, "wb") as f:
        f.write(archivo.file.read())
    fontanero.foto_url = f"/uploads/{nombre_archivo}"
    db.commit()
    return {"foto_url": fontanero.foto_url}

@app.get("/fontaneros/{fontanero_id}/perfil", response_model=schemas.FontaneroRespuesta)
def ver_perfil_fontanero(fontanero_id: int, db: Session = Depends(get_db)):
    fontanero = db.query(models.Fontanero).filter(
        models.Fontanero.usuario_id == fontanero_id
    ).first()
    if not fontanero:
        raise HTTPException(status_code=404, detail="Fontanero no encontrado")
    return fontanero

# ─── VACACIONES ────────────────────────────────────────────────────────────────

@app.put("/fontaneros/{fontanero_id}/vacaciones")
def activar_vacaciones(
    fontanero_id: int,
    datos: schemas.VacacionesCrear,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    fontanero = db.query(models.Fontanero).filter(
        models.Fontanero.usuario_id == fontanero_id
    ).first()
    if not fontanero:
        raise HTTPException(status_code=404, detail="Fontanero no encontrado")
    fontanero.vacaciones_desde = datos.desde
    fontanero.vacaciones_hasta = datos.hasta
    fontanero.disponible = False
    db.commit()
    return {"mensaje": "Modo vacaciones activado"}

@app.delete("/fontaneros/{fontanero_id}/vacaciones")
def cancelar_vacaciones(
    fontanero_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    fontanero = db.query(models.Fontanero).filter(
        models.Fontanero.usuario_id == fontanero_id
    ).first()
    if not fontanero:
        raise HTTPException(status_code=404, detail="Fontanero no encontrado")
    fontanero.vacaciones_desde = None
    fontanero.vacaciones_hasta = None
    fontanero.disponible = True
    db.commit()
    return {"mensaje": "Modo vacaciones cancelado"}

# ─── GALERÍA DE TRABAJOS ───────────────────────────────────────────────────────

@app.post("/fontaneros/{fontanero_id}/galeria", response_model=schemas.GaleriaRespuesta)
def subir_foto_galeria(
    fontanero_id: int,
    descripcion: Optional[str] = None,
    archivo: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    fontanero = db.query(models.Fontanero).filter(
        models.Fontanero.usuario_id == fontanero_id
    ).first()
    if not fontanero:
        raise HTTPException(status_code=404, detail="Fontanero no encontrado")
    if archivo.content_type not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(status_code=400, detail="Tipo de archivo no permitido")
    ext = archivo.filename.rsplit(".", 1)[-1] if "." in archivo.filename else "jpg"
    nombre_archivo = f"galeria_{uuid.uuid4().hex}.{ext}"
    ruta = os.path.join(UPLOAD_DIR, nombre_archivo)
    with open(ruta, "wb") as f:
        f.write(archivo.file.read())
    foto = models.GaleriaFontanero(
        fontanero_id=fontanero.id,
        url=f"/uploads/{nombre_archivo}",
        descripcion=descripcion,
    )
    db.add(foto)
    db.commit()
    db.refresh(foto)
    return foto

@app.get("/fontaneros/{fontanero_id}/galeria", response_model=List[schemas.GaleriaRespuesta])
def ver_galeria(fontanero_id: int, db: Session = Depends(get_db)):
    fontanero = db.query(models.Fontanero).filter(
        models.Fontanero.usuario_id == fontanero_id
    ).first()
    if not fontanero:
        raise HTTPException(status_code=404, detail="Fontanero no encontrado")
    return db.query(models.GaleriaFontanero).filter(
        models.GaleriaFontanero.fontanero_id == fontanero.id
    ).order_by(models.GaleriaFontanero.creado_en.desc()).all()

@app.delete("/fontaneros/{fontanero_id}/galeria/{foto_id}")
def eliminar_foto_galeria(
    fontanero_id: int,
    foto_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    fontanero = db.query(models.Fontanero).filter(
        models.Fontanero.usuario_id == fontanero_id
    ).first()
    if not fontanero:
        raise HTTPException(status_code=404, detail="Fontanero no encontrado")
    foto = db.query(models.GaleriaFontanero).filter(
        models.GaleriaFontanero.id == foto_id,
        models.GaleriaFontanero.fontanero_id == fontanero.id,
    ).first()
    if not foto:
        raise HTTPException(status_code=404, detail="Foto no encontrada")
    if os.path.exists(foto.url.lstrip("/")):
        os.remove(foto.url.lstrip("/"))
    db.delete(foto)
    db.commit()
    return {"mensaje": "Foto eliminada"}

# ─── ESTADÍSTICAS ──────────────────────────────────────────────────────────────

@app.get("/fontaneros/{fontanero_id}/estadisticas", response_model=schemas.EstadisticasRespuesta)
def ver_estadisticas(
    fontanero_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    fontanero = db.query(models.Fontanero).filter(
        models.Fontanero.usuario_id == fontanero_id
    ).first()
    if not fontanero:
        raise HTTPException(status_code=404, detail="Fontanero no encontrado")

    servicios_pagados = db.query(models.Servicio).filter(
        models.Servicio.fontanero_id == fontanero.id,
        models.Servicio.estado == "pagado",
    ).all()

    trabajos_completados = len(servicios_pagados)
    ingresos_totales = sum(s.precio for s in servicios_pagados if s.precio) * 0.95

    aceptados = db.query(models.Servicio).filter(
        models.Servicio.fontanero_id == fontanero.id,
        models.Servicio.estado.in_(["aceptado", "precio_enviado", "pagado", "pago_pendiente"]),
    ).count()
    rechazados = db.query(models.Servicio).filter(
        models.Servicio.fontanero_id == fontanero.id,
        models.Servicio.estado == "rechazado",
    ).count()
    total = aceptados + rechazados
    tasa_aceptacion = round(aceptados / total * 100, 1) if total > 0 else 100.0

    return {
        "trabajos_completados": trabajos_completados,
        "ingresos_totales": round(ingresos_totales, 2),
        "valoracion_media": fontanero.valoracion or 5.0,
        "tasa_aceptacion": tasa_aceptacion,
    }

# ─── BÚSQUEDA Y FILTROS ────────────────────────────────────────────────────────

@app.get("/buscar/fontaneros", response_model=List[schemas.FontaneroRespuesta])
def buscar_fontaneros(
    zona: Optional[str] = None,
    gremio: Optional[str] = None,
    disponible_24h: Optional[bool] = None,
    verificado: Optional[bool] = None,
    db: Session = Depends(get_db),
):
    q = db.query(models.Fontanero).filter(models.Fontanero.disponible == True)
    if zona:
        q = q.filter(models.Fontanero.zona.ilike(f"%{zona}%"))
    if gremio:
        q = q.filter(models.Fontanero.gremio == gremio)
    if disponible_24h is not None:
        q = q.filter(models.Fontanero.disponible_24h == disponible_24h)
    if verificado is not None:
        q = q.filter(models.Fontanero.verificado == verificado)
    return q.order_by(models.Fontanero.valoracion.desc()).all()

# ─── ETA ───────────────────────────────────────────────────────────────────────

@app.put("/servicios/{servicio_id}/eta")
def actualizar_eta(
    servicio_id: int,
    datos: schemas.ETAUpdate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    servicio = db.query(models.Servicio).filter(models.Servicio.id == servicio_id).first()
    if not servicio:
        raise HTTPException(status_code=404, detail="Servicio no encontrado")
    fontanero = db.query(models.Fontanero).filter(
        models.Fontanero.usuario_id == current_user["id"]
    ).first()
    if not fontanero or servicio.fontanero_id != fontanero.id:
        raise HTTPException(status_code=403, detail="No puedes actualizar el ETA de un servicio que no es tuyo")
    servicio.eta_minutos = datos.eta_minutos
    _crear_notificacion(db, servicio.cliente_id, "Fontanero en camino", f"Llegará en aproximadamente {datos.eta_minutos} minutos", "eta", servicio_id)
    db.commit()
    return {"mensaje": "ETA actualizado", "eta_minutos": datos.eta_minutos}

# ─── IA CLASIFICACIÓN URGENCIA ────────────────────────────────────────────────

PALABRAS_CRITICA = ["inundación", "inundacion", "rotura", "reventado", "agua por todas", "escape de gas", "gas"]
PALABRAS_ALTA = ["fuga", "goteo fuerte", "sin agua", "tubería rota", "tuberia rota", "urgente"]
PALABRAS_MEDIA = ["goteo", "grifo", "presión baja", "presion baja", "atasco"]

def _clasificar_urgencia(texto: str) -> str:
    t = (texto or "").lower()
    if any(p in t for p in PALABRAS_CRITICA):
        return "critica"
    if any(p in t for p in PALABRAS_ALTA):
        return "alta"
    if any(p in t for p in PALABRAS_MEDIA):
        return "media"
    return "baja"

@app.post("/servicios/{servicio_id}/clasificar-urgencia")
def clasificar_urgencia(
    servicio_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    servicio = db.query(models.Servicio).filter(models.Servicio.id == servicio_id).first()
    if not servicio:
        raise HTTPException(status_code=404, detail="Servicio no encontrado")
    nivel = _clasificar_urgencia(f"{servicio.tipo} {servicio.descripcion or ''}")
    servicio.urgencia_ia = nivel
    db.commit()
    return {"urgencia_ia": nivel}

# ─── FAVORITOS ─────────────────────────────────────────────────────────────────

@app.post("/clientes/{cliente_id}/favoritos/{fontanero_id}")
def agregar_favorito(
    cliente_id: int,
    fontanero_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    existe = db.query(models.Favorito).filter(
        models.Favorito.cliente_id == cliente_id,
        models.Favorito.fontanero_id == fontanero_id,
    ).first()
    if existe:
        return {"mensaje": "Ya está en favoritos"}
    db.add(models.Favorito(cliente_id=cliente_id, fontanero_id=fontanero_id))
    db.commit()
    return {"mensaje": "Añadido a favoritos"}

@app.delete("/clientes/{cliente_id}/favoritos/{fontanero_id}")
def eliminar_favorito(
    cliente_id: int,
    fontanero_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    fav = db.query(models.Favorito).filter(
        models.Favorito.cliente_id == cliente_id,
        models.Favorito.fontanero_id == fontanero_id,
    ).first()
    if fav:
        db.delete(fav)
        db.commit()
    return {"mensaje": "Eliminado de favoritos"}

@app.get("/clientes/{cliente_id}/favoritos", response_model=List[schemas.FontaneroRespuesta])
def listar_favoritos(
    cliente_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    favs = db.query(models.Favorito).filter(models.Favorito.cliente_id == cliente_id).all()
    ids = [f.fontanero_id for f in favs]
    return db.query(models.Fontanero).filter(models.Fontanero.id.in_(ids)).all()

# ─── SISTEMA DE LICITACIÓN (OFERTAS) ──────────────────────────────────────────

def _enriquecer_oferta(db: Session, oferta: models.Oferta, incluir_fontanero: bool = False, incluir_servicio: bool = False) -> schemas.OfertaRespuesta:
    data = schemas.OfertaRespuesta.model_validate(oferta)
    if incluir_fontanero:
        fontanero = db.query(models.Fontanero).filter(models.Fontanero.id == oferta.fontanero_id).first()
        if fontanero:
            data.fontanero_nombre = fontanero.nombre
            data.fontanero_valoracion = fontanero.valoracion
            data.fontanero_zona = fontanero.zona
            data.fontanero_trabajos = fontanero.num_trabajos
    if incluir_servicio:
        servicio = db.query(models.Servicio).filter(models.Servicio.id == oferta.servicio_id).first()
        if servicio:
            data.tipo = servicio.tipo
            data.zona = "Bilbao"
    return data

@app.post("/servicios/{servicio_id}/ofertas", response_model=schemas.OfertaRespuesta)
def crear_oferta(
    servicio_id: int,
    datos: schemas.OfertaCrear,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    servicio = db.query(models.Servicio).filter(models.Servicio.id == servicio_id).first()
    if not servicio:
        raise HTTPException(status_code=404, detail="Servicio no encontrado")
    fontanero = db.query(models.Fontanero).filter(
        models.Fontanero.usuario_id == current_user["id"]
    ).first()
    if not fontanero:
        raise HTTPException(status_code=404, detail="Fontanero no encontrado")
    existente = db.query(models.Oferta).filter(
        models.Oferta.servicio_id == servicio_id,
        models.Oferta.fontanero_id == fontanero.id,
    ).first()
    if existente:
        existente.precio = datos.precio
        existente.mensaje = datos.mensaje
        db.commit()
        db.refresh(existente)
        return existente
    oferta = models.Oferta(servicio_id=servicio_id, fontanero_id=fontanero.id, precio=datos.precio, mensaje=datos.mensaje)
    db.add(oferta)
    _crear_notificacion(db, servicio.cliente_id, "Nueva oferta recibida", f"Un profesional ofrece realizar el servicio por {datos.precio}€", "oferta", servicio_id)
    db.commit()
    db.refresh(oferta)
    return oferta

@app.get("/servicios/{servicio_id}/ofertas", response_model=List[schemas.OfertaRespuesta])
def listar_ofertas(
    servicio_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    ofertas = db.query(models.Oferta).filter(
        models.Oferta.servicio_id == servicio_id,
        models.Oferta.estado == "pendiente",
    ).order_by(models.Oferta.precio).all()
    return [_enriquecer_oferta(db, o, incluir_fontanero=True) for o in ofertas]

@app.get("/fontaneros/{fontanero_id}/ofertas", response_model=List[schemas.OfertaRespuesta])
def listar_mis_ofertas(
    fontanero_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    fontanero = _resolver_fontanero(db, fontanero_id)
    ofertas = db.query(models.Oferta).filter(
        models.Oferta.fontanero_id == fontanero.id
    ).order_by(models.Oferta.id.desc()).all()
    return [_enriquecer_oferta(db, o, incluir_servicio=True) for o in ofertas]

@app.put("/servicios/{servicio_id}/ofertas/{oferta_id}/aceptar")
def aceptar_oferta(
    servicio_id: int,
    oferta_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    oferta = db.query(models.Oferta).filter(
        models.Oferta.id == oferta_id,
        models.Oferta.servicio_id == servicio_id,
    ).first()
    if not oferta:
        raise HTTPException(status_code=404, detail="Oferta no encontrada")
    servicio = db.query(models.Servicio).filter(models.Servicio.id == servicio_id).first()
    if not servicio:
        raise HTTPException(status_code=404, detail="Servicio no encontrado")
    if servicio.cliente_id != current_user["id"]:
        raise HTTPException(status_code=403, detail="Solo el cliente puede aceptar ofertas de este servicio")
    servicio.fontanero_id = oferta.fontanero_id
    servicio.precio = oferta.precio
    servicio.estado = "aceptado"
    oferta.estado = "aceptada"
    db.query(models.Oferta).filter(
        models.Oferta.servicio_id == servicio_id,
        models.Oferta.id != oferta_id,
    ).update({"estado": "rechazada"})
    fontanero = db.query(models.Fontanero).filter(models.Fontanero.id == oferta.fontanero_id).first()
    if fontanero and fontanero.usuario_id:
        _crear_notificacion(db, fontanero.usuario_id, "¡Tu oferta fue aceptada!", f"El cliente aceptó tu oferta de {oferta.precio}€", "oferta_aceptada", servicio_id)
    db.commit()
    return {"mensaje": "Oferta aceptada"}

@app.put("/servicios/{servicio_id}/ofertas/{oferta_id}/rechazar")
def rechazar_oferta(
    servicio_id: int,
    oferta_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    oferta = db.query(models.Oferta).filter(
        models.Oferta.id == oferta_id,
        models.Oferta.servicio_id == servicio_id,
    ).first()
    if not oferta:
        raise HTTPException(status_code=404, detail="Oferta no encontrada")
    servicio = db.query(models.Servicio).filter(models.Servicio.id == servicio_id).first()
    if not servicio:
        raise HTTPException(status_code=404, detail="Servicio no encontrado")
    if servicio.cliente_id != current_user["id"]:
        raise HTTPException(status_code=403, detail="Solo el cliente puede rechazar ofertas de este servicio")
    oferta.estado = "rechazada"
    db.commit()
    return {"mensaje": "Oferta rechazada"}

# ─── VALORACIÓN DETALLADA ─────────────────────────────────────────────────────

@app.post("/servicios/{servicio_id}/resena", response_model=schemas.ResenaRespuesta)
def crear_resena(
    servicio_id: int,
    datos: schemas.ResenaCrear,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    servicio = db.query(models.Servicio).filter(models.Servicio.id == servicio_id).first()
    if not servicio:
        raise HTTPException(status_code=404, detail="Servicio no encontrado")
    if servicio.estado != "pagado":
        raise HTTPException(status_code=400, detail="Solo se puede reseñar servicios pagados")
    if servicio.cliente_id != current_user["id"]:
        raise HTTPException(status_code=403, detail="Solo el cliente puede reseñar")
    existente = db.query(models.Resena).filter(models.Resena.servicio_id == servicio_id).first()
    if existente:
        raise HTTPException(status_code=400, detail="Ya existe una reseña para este servicio")
    media = round((datos.puntualidad + datos.calidad + datos.precio_justo + datos.trato) / 4, 2)
    resena = models.Resena(
        servicio_id=servicio_id,
        cliente_id=current_user["id"],
        fontanero_id=servicio.fontanero_id,
        puntualidad=datos.puntualidad,
        calidad=datos.calidad,
        precio_justo=datos.precio_justo,
        trato=datos.trato,
        comentario=datos.comentario,
    )
    db.add(resena)
    fontanero = db.query(models.Fontanero).filter(models.Fontanero.id == servicio.fontanero_id).first()
    if fontanero:
        resenas = db.query(models.Resena).filter(models.Resena.fontanero_id == fontanero.id).all()
        total = sum((r.puntualidad + r.calidad + r.precio_justo + r.trato) / 4 for r in resenas)
        fontanero.valoracion = round((total + media) / (len(resenas) + 1), 2)
        fontanero.num_trabajos = (fontanero.num_trabajos or 0) + 1
        if fontanero.usuario_id:
            _crear_notificacion(db, fontanero.usuario_id, "Nueva reseña recibida", f"Has recibido una valoración de {media}/5", "resena", servicio_id)
    db.commit()
    db.refresh(resena)
    return resena

@app.get("/fontaneros/{fontanero_id}/resenas", response_model=List[schemas.ResenaRespuesta])
def ver_resenas(fontanero_id: int, db: Session = Depends(get_db)):
    fontanero = db.query(models.Fontanero).filter(models.Fontanero.usuario_id == fontanero_id).first()
    if not fontanero:
        raise HTTPException(status_code=404, detail="Fontanero no encontrado")
    return db.query(models.Resena).filter(
        models.Resena.fontanero_id == fontanero.id
    ).order_by(models.Resena.creado_en.desc()).all()

# ─── CALENDARIO INTERNO ───────────────────────────────────────────────────────

@app.post("/fontaneros/{fontanero_id}/citas", response_model=schemas.CitaRespuesta)
def crear_cita(
    fontanero_id: int,
    datos: schemas.CitaCrear,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    fontanero = db.query(models.Fontanero).filter(models.Fontanero.usuario_id == fontanero_id).first()
    if not fontanero:
        raise HTTPException(status_code=404, detail="Fontanero no encontrado")
    cita = models.Cita(
        fontanero_id=fontanero.id,
        servicio_id=datos.servicio_id,
        titulo=datos.titulo,
        fecha_inicio=datos.fecha_inicio,
        fecha_fin=datos.fecha_fin,
    )
    db.add(cita)
    db.commit()
    db.refresh(cita)
    return cita

@app.get("/fontaneros/{fontanero_id}/citas", response_model=List[schemas.CitaRespuesta])
def ver_citas(
    fontanero_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    fontanero = db.query(models.Fontanero).filter(models.Fontanero.usuario_id == fontanero_id).first()
    if not fontanero:
        raise HTTPException(status_code=404, detail="Fontanero no encontrado")
    return db.query(models.Cita).filter(
        models.Cita.fontanero_id == fontanero.id
    ).order_by(models.Cita.fecha_inicio).all()

@app.delete("/fontaneros/{fontanero_id}/citas/{cita_id}")
def eliminar_cita(
    fontanero_id: int,
    cita_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    fontanero = db.query(models.Fontanero).filter(models.Fontanero.usuario_id == fontanero_id).first()
    if not fontanero:
        raise HTTPException(status_code=404, detail="Fontanero no encontrado")
    cita = db.query(models.Cita).filter(
        models.Cita.id == cita_id,
        models.Cita.fontanero_id == fontanero.id,
    ).first()
    if not cita:
        raise HTTPException(status_code=404, detail="Cita no encontrada")
    db.delete(cita)
    db.commit()
    return {"mensaje": "Cita eliminada"}

# ─── HISTORIAL DE PAGOS FONTANERO ─────────────────────────────────────────────

@app.get("/fontaneros/{fontanero_id}/pagos")
def historial_pagos(
    fontanero_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    fontanero = db.query(models.Fontanero).filter(models.Fontanero.usuario_id == fontanero_id).first()
    if not fontanero:
        raise HTTPException(status_code=404, detail="Fontanero no encontrado")
    servicios = db.query(models.Servicio).filter(
        models.Servicio.fontanero_id == fontanero.id,
        models.Servicio.estado == "pagado",
        models.Servicio.precio != None,
    ).order_by(models.Servicio.creado_en.desc()).all()
    resultado = []
    for s in servicios:
        cliente = db.query(models.Usuario).filter(models.Usuario.id == s.cliente_id).first()
        comision = round(s.precio * 0.05, 2)
        resultado.append({
            "servicio_id": s.id,
            "tipo": s.tipo,
            "fecha": str(s.creado_en),
            "precio_bruto": s.precio,
            "comision_plataforma": comision,
            "precio_neto": round(s.precio - comision, 2),
            "metodo_pago": s.metodo_pago,
            "cliente": cliente.nombre if cliente else "Cliente",
        })
    return resultado

# ─── FACTURA PDF ──────────────────────────────────────────────────────────────

@app.get("/servicios/{servicio_id}/factura")
def descargar_factura(
    servicio_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    from fastapi.responses import Response
    try:
        from reportlab.pdfgen import canvas
        from reportlab.lib.pagesizes import A4
        import io
    except ImportError:
        raise HTTPException(status_code=501, detail="Generación de PDF no disponible. Instala reportlab.")

    servicio = db.query(models.Servicio).filter(models.Servicio.id == servicio_id).first()
    if not servicio:
        raise HTTPException(status_code=404, detail="Servicio no encontrado")
    if servicio.estado not in ["pagado", "pago_pendiente"]:
        raise HTTPException(status_code=400, detail="Solo se puede generar factura de servicios pagados")

    cliente = db.query(models.Usuario).filter(models.Usuario.id == servicio.cliente_id).first()
    fontanero_obj = db.query(models.Fontanero).filter(models.Fontanero.id == servicio.fontanero_id).first() if servicio.fontanero_id else None

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    w, h = A4
    c.setFont("Helvetica-Bold", 20)
    c.drawString(50, h - 60, "FonTap - Factura")
    c.setFont("Helvetica", 12)
    c.drawString(50, h - 100, f"Nº Factura: {servicio_id:05d}")
    c.drawString(50, h - 120, f"Fecha: {servicio.creado_en.strftime('%d/%m/%Y') if servicio.creado_en else '-'}")
    c.drawString(50, h - 160, f"Cliente: {cliente.nombre if cliente else '-'}")
    c.drawString(50, h - 180, f"Email: {cliente.email if cliente else '-'}")
    c.drawString(50, h - 220, f"Profesional: {fontanero_obj.nombre if fontanero_obj else '-'}")
    c.drawString(50, h - 240, f"Zona: {fontanero_obj.zona if fontanero_obj else '-'}")
    c.drawString(50, h - 280, f"Servicio: {servicio.tipo}")
    c.drawString(50, h - 300, f"Descripción: {servicio.descripcion or '-'}")
    c.drawString(50, h - 340, f"Importe total: {servicio.precio or 0:.2f} EUR")
    c.drawString(50, h - 360, f"Método de pago: {servicio.metodo_pago or '-'}")
    c.setFont("Helvetica-Bold", 10)
    c.drawString(50, 50, "FonTap - Plataforma de servicios del hogar")
    c.save()
    buf.seek(0)
    return Response(content=buf.read(), media_type="application/pdf", headers={"Content-Disposition": f"attachment; filename=factura_{servicio_id}.pdf"})

# ─── STRIPE ───────────────────────────────────────────────────────────────────

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")

@app.post("/servicios/{servicio_id}/stripe/crear-intent")
def crear_stripe_intent(
    servicio_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    if not STRIPE_SECRET_KEY:
        raise HTTPException(status_code=501, detail="Stripe no configurado. Añade STRIPE_SECRET_KEY en variables de entorno.")
    try:
        import stripe
        stripe.api_key = STRIPE_SECRET_KEY
    except ImportError:
        raise HTTPException(status_code=501, detail="Librería stripe no instalada.")

    servicio = db.query(models.Servicio).filter(models.Servicio.id == servicio_id).first()
    if not servicio or not servicio.precio:
        raise HTTPException(status_code=400, detail="Servicio no encontrado o sin precio")
    intent = stripe.PaymentIntent.create(
        amount=int(servicio.precio * 100),
        currency="eur",
        metadata={"servicio_id": servicio_id},
    )
    servicio.stripe_payment_intent = intent.id
    db.commit()
    return {"client_secret": intent.client_secret, "amount": servicio.precio}

@app.post("/servicios/{servicio_id}/stripe/confirmar")
def confirmar_stripe(
    servicio_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    servicio = db.query(models.Servicio).filter(models.Servicio.id == servicio_id).first()
    if not servicio:
        raise HTTPException(status_code=404, detail="Servicio no encontrado")
    servicio.estado = "pagado"
    servicio.metodo_pago = "stripe"
    comision = round((servicio.precio or 0) * 0.05, 2)
    servicio.comision_aplicada = comision
    if servicio.fontanero_id:
        fontanero_obj = db.query(models.Fontanero).filter(models.Fontanero.id == servicio.fontanero_id).first()
        if fontanero_obj and fontanero_obj.usuario_id:
            _crear_notificacion(db, fontanero_obj.usuario_id, "Pago recibido por Stripe", f"Pago de {servicio.precio}€ confirmado", "pago_recibido", servicio_id)
    db.commit()
    return {"mensaje": "Pago confirmado", "precio": servicio.precio, "comision": comision}

# ─── BIZUM ────────────────────────────────────────────────────────────────────

@app.get("/servicios/{servicio_id}/bizum/instrucciones")
def instrucciones_bizum(
    servicio_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    servicio = db.query(models.Servicio).filter(models.Servicio.id == servicio_id).first()
    if not servicio or not servicio.precio:
        raise HTTPException(status_code=400, detail="Servicio no encontrado o sin precio")
    fontanero_obj = db.query(models.Fontanero).filter(models.Fontanero.id == servicio.fontanero_id).first() if servicio.fontanero_id else None
    usuario_fontanero = db.query(models.Usuario).filter(models.Usuario.id == fontanero_obj.usuario_id).first() if fontanero_obj else None
    return {
        "importe": servicio.precio,
        "concepto": f"FonTap servicio #{servicio_id}",
        "telefono_destino": usuario_fontanero.telefono if usuario_fontanero else "Consultar con el profesional",
        "instrucciones": [
            f"1. Abre tu app bancaria y selecciona Bizum",
            f"2. Envía {servicio.precio}€ al teléfono del profesional",
            f"3. Añade el concepto: FonTap servicio #{servicio_id}",
            f"4. Vuelve a la app y confirma el pago",
        ],
    }

# ─── INMUEBLES (ADMIN FINCAS) ─────────────────────────────────────────────────

@app.post("/inmuebles", response_model=schemas.InmuebleRespuesta)
def crear_inmueble(
    datos: schemas.InmuebleCrear,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    inmueble = models.Inmueble(
        administrador_id=current_user["id"],
        nombre=datos.nombre,
        direccion=datos.direccion,
        ciudad=datos.ciudad,
    )
    db.add(inmueble)
    db.commit()
    db.refresh(inmueble)
    return inmueble

@app.get("/inmuebles", response_model=List[schemas.InmuebleRespuesta])
def listar_inmuebles(
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    return db.query(models.Inmueble).filter(
        models.Inmueble.administrador_id == current_user["id"]
    ).all()

@app.delete("/inmuebles/{inmueble_id}")
def eliminar_inmueble(
    inmueble_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    inmueble = db.query(models.Inmueble).filter(
        models.Inmueble.id == inmueble_id,
        models.Inmueble.administrador_id == current_user["id"],
    ).first()
    if not inmueble:
        raise HTTPException(status_code=404, detail="Inmueble no encontrado")
    db.delete(inmueble)
    db.commit()
    return {"mensaje": "Inmueble eliminado"}

# ─── VERIFICACIÓN DE IDENTIDAD ────────────────────────────────────────────────

@app.post("/fontaneros/{fontanero_id}/documentos", response_model=schemas.DocumentoRespuesta)
def subir_documento(
    fontanero_id: int,
    tipo: str,
    archivo: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    fontanero = db.query(models.Fontanero).filter(models.Fontanero.usuario_id == fontanero_id).first()
    if not fontanero:
        raise HTTPException(status_code=404, detail="Fontanero no encontrado")
    ext = archivo.filename.rsplit(".", 1)[-1] if "." in archivo.filename else "pdf"
    nombre_archivo = f"doc_{uuid.uuid4().hex}.{ext}"
    ruta = os.path.join(UPLOAD_DIR, nombre_archivo)
    with open(ruta, "wb") as f:
        f.write(archivo.file.read())
    doc = models.DocumentoVerificacion(
        fontanero_id=fontanero.id,
        tipo=tipo,
        url=f"/uploads/{nombre_archivo}",
        estado="pendiente",
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)
    return doc

@app.get("/fontaneros/{fontanero_id}/documentos", response_model=List[schemas.DocumentoRespuesta])
def ver_documentos(
    fontanero_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    fontanero = db.query(models.Fontanero).filter(models.Fontanero.usuario_id == fontanero_id).first()
    if not fontanero:
        raise HTTPException(status_code=404, detail="Fontanero no encontrado")
    return db.query(models.DocumentoVerificacion).filter(
        models.DocumentoVerificacion.fontanero_id == fontanero.id
    ).all()

# ─── PANEL DE ADMINISTRACIÓN ──────────────────────────────────────────────────

def _verificar_admin(current_user: dict):
    if current_user.get("tipo") not in ["admin"]:
        raise HTTPException(status_code=403, detail="Acceso solo para administradores")

@app.get("/admin/estadisticas", response_model=schemas.AdminStats)
def admin_estadisticas(
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    _verificar_admin(current_user)
    total_usuarios = db.query(models.Usuario).count()
    total_fontaneros = db.query(models.Usuario).filter(models.Usuario.tipo == "fontanero").count()
    total_clientes = db.query(models.Usuario).filter(models.Usuario.tipo == "cliente").count()
    total_servicios = db.query(models.Servicio).count()
    servicios_pendientes = db.query(models.Servicio).filter(models.Servicio.estado == "pendiente").count()
    servicios_completados = db.query(models.Servicio).filter(models.Servicio.estado == "pagado").count()
    pagados = db.query(models.Servicio).filter(
        models.Servicio.estado == "pagado",
        models.Servicio.precio != None,
    ).all()
    ingresos = sum(s.precio * 0.05 for s in pagados if s.precio)
    return {
        "total_usuarios": total_usuarios,
        "total_fontaneros": total_fontaneros,
        "total_clientes": total_clientes,
        "total_servicios": total_servicios,
        "servicios_pendientes": servicios_pendientes,
        "servicios_completados": servicios_completados,
        "ingresos_plataforma": round(ingresos, 2),
    }

@app.get("/admin/usuarios", response_model=List[schemas.UsuarioRespuesta])
def admin_listar_usuarios(
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    _verificar_admin(current_user)
    return db.query(models.Usuario).order_by(models.Usuario.creado_en.desc()).all()

@app.get("/admin/servicios")
def admin_listar_servicios(
    estado: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    _verificar_admin(current_user)
    q = db.query(models.Servicio)
    if estado:
        q = q.filter(models.Servicio.estado == estado)
    return q.order_by(models.Servicio.creado_en.desc()).all()

@app.put("/admin/fontaneros/{fontanero_id}/verificar")
def admin_verificar_fontanero(
    fontanero_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    _verificar_admin(current_user)
    fontanero = db.query(models.Fontanero).filter(models.Fontanero.id == fontanero_id).first()
    if not fontanero:
        raise HTTPException(status_code=404, detail="Fontanero no encontrado")
    fontanero.verificado = True
    if fontanero.usuario_id:
        _crear_notificacion(db, fontanero.usuario_id, "¡Perfil verificado!", "Tu perfil ha sido verificado por FonTap", "verificacion", fontanero_id)
    db.commit()
    return {"mensaje": "Fontanero verificado"}

@app.put("/admin/documentos/{doc_id}/revisar")
def admin_revisar_documento(
    doc_id: int,
    estado: str,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    _verificar_admin(current_user)
    if estado not in ["verificado", "rechazado"]:
        raise HTTPException(status_code=400, detail="Estado debe ser verificado o rechazado")
    doc = db.query(models.DocumentoVerificacion).filter(models.DocumentoVerificacion.id == doc_id).first()
    if not doc:
        raise HTTPException(status_code=404, detail="Documento no encontrado")
    doc.estado = estado
    fontanero = db.query(models.Fontanero).filter(models.Fontanero.id == doc.fontanero_id).first()
    if fontanero and fontanero.usuario_id:
        msg = "aprobado" if estado == "verificado" else "rechazado"
        _crear_notificacion(db, fontanero.usuario_id, f"Documento {msg}", f"Tu documento '{doc.tipo}' ha sido {msg}", "documento", doc_id)
    db.commit()
    return {"mensaje": f"Documento {estado}"}

# ─── MENSAJES CHAT ────────────────────────────────────────────────────────────

class MensajeCrear(BaseModel):
    contenido: str

@app.post("/servicios/{servicio_id}/mensajes")
def enviar_mensaje(
    servicio_id: int,
    mensaje: MensajeCrear,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    servicio = db.query(models.Servicio).filter(models.Servicio.id == servicio_id).first()
    if not servicio:
        raise HTTPException(status_code=404, detail="Servicio no encontrado")
    emisor_id = current_user["id"]
    emisor = db.query(models.Usuario).filter(models.Usuario.id == emisor_id).first()
    nuevo = models.Mensaje(
        servicio_id=servicio_id,
        emisor_id=emisor_id,
        texto=mensaje.contenido,
        leido=False,
    )
    db.add(nuevo)
    if emisor_id == servicio.cliente_id and servicio.fontanero_id:
        fontanero_obj = db.query(models.Fontanero).filter(models.Fontanero.id == servicio.fontanero_id).first()
        if fontanero_obj and fontanero_obj.usuario_id:
            _crear_notificacion(db, fontanero_obj.usuario_id, "Nuevo mensaje", mensaje.contenido[:80], "mensaje", servicio_id)
    elif servicio.cliente_id != emisor_id:
        _crear_notificacion(db, servicio.cliente_id, "Nuevo mensaje", mensaje.contenido[:80], "mensaje", servicio_id)
    db.commit()
    db.refresh(nuevo)
    return {
        "id": nuevo.id,
        "contenido": nuevo.texto,
        "remitente_tipo": emisor.tipo if emisor else "cliente",
        "remitente_nombre": emisor.nombre if emisor else "Usuario",
        "creado_en": str(nuevo.creado_en),
    }

@app.get("/servicios/{servicio_id}/mensajes")
def ver_mensajes(
    servicio_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    mensajes = db.query(models.Mensaje).filter(
        models.Mensaje.servicio_id == servicio_id
    ).order_by(models.Mensaje.creado_en).all()
    resultado = []
    for m in mensajes:
        emisor = db.query(models.Usuario).filter(models.Usuario.id == m.emisor_id).first()
        resultado.append({
            "id": m.id,
            "contenido": m.texto,
            "remitente_tipo": emisor.tipo if emisor else "cliente",
            "remitente_nombre": emisor.nombre if emisor else "Usuario",
            "creado_en": str(m.creado_en),
        })
    return resultado
# ─── CHAT GENERAL ─────────────────────────────────────────────────────────────

class MensajeChatCrear(BaseModel):
    contenido: str
    remitente_tipo: str
    remitente_nombre: Optional[str] = None

def _escapar_like(valor: str) -> str:
    return valor.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

@app.post("/chat/{chat_id}/mensajes")
def enviar_mensaje_chat(
    chat_id: str,
    mensaje: MensajeChatCrear,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    nombre = mensaje.remitente_nombre or "Usuario"
    nuevo = models.Mensaje(
        servicio_id=0,
        emisor_id=current_user["id"],
        texto=f"{chat_id}||{mensaje.remitente_tipo}||{nombre}||{mensaje.contenido}",
        leido=False,
    )
    db.add(nuevo)
    db.commit()
    db.refresh(nuevo)
    return {
        "id": nuevo.id,
        "contenido": mensaje.contenido,
        "remitente_tipo": mensaje.remitente_tipo,
        "remitente_nombre": nombre,
        "creado_en": str(nuevo.creado_en),
    }

@app.get("/chat/{chat_id}/mensajes")
def ver_mensajes_chat(
    chat_id: str,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    mensajes = db.query(models.Mensaje).filter(
        models.Mensaje.texto.like(f"{_escapar_like(chat_id)}||%", escape="\\")
    ).order_by(models.Mensaje.creado_en).all()
    resultado = []
    for m in mensajes:
        partes = m.texto.split("||", 3)
        if len(partes) == 4:
            resultado.append({
                "id": m.id,
                "contenido": partes[3],
                "remitente_tipo": partes[1],
                "remitente_nombre": partes[2],
                "creado_en": str(m.creado_en),
            })
    return resultado