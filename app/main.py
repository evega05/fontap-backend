from fastapi import FastAPI, Depends, HTTPException, status, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
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
        models.Servicio.fontanero_id == None,
    ).all()

    propias = db.query(models.Servicio).filter(
        models.Servicio.fontanero_id == fontanero.id,
        models.Servicio.estado.in_(["aceptado", "completado", "pagado"]),
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
    if servicio.urgente:
        fontaneros_zona = db.query(models.Fontanero).filter(
            models.Fontanero.disponible == True,
            models.Fontanero.usuario_id != None,
        ).all()
        for f in fontaneros_zona:
            _crear_notificacion(db, f.usuario_id, "Nueva solicitud urgente", f"Solicitud urgente de {servicio.tipo} cerca de tu zona", "solicitud_urgente", nuevo.id)
        db.commit()
    return nuevo

@app.get("/servicios/{servicio_id}")
def ver_servicio(
    servicio_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    servicio = db.query(models.Servicio).filter(models.Servicio.id == servicio_id).first()
    if not servicio:
        raise HTTPException(status_code=404, detail="Servicio no encontrado")
    return {
        "id": servicio.id,
        "tipo": servicio.tipo,
        "descripcion": servicio.descripcion,
        "urgente": servicio.urgente,
        "estado": servicio.estado,
        "precio": servicio.precio,
        "fecha": str(servicio.fecha) if servicio.fecha else None,
    }

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
    return db.query(models.Servicio).filter(
        models.Servicio.cliente_id == cliente_id
    ).order_by(models.Servicio.id.desc()).all()

# ─── HORARIOS / BLOQUEOS / SERVICIOS FONTANERO ────────────────────────────────

@app.post("/fontaneros/{fontanero_id}/horario", response_model=schemas.HorarioBaseRespuesta)
def crear_horario(
    fontanero_id: int,
    horario: schemas.HorarioBaseCrear,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    nuevo = models.HorarioBase(
        fontanero_id=fontanero_id,
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
    return db.query(models.HorarioBase).filter(
        models.HorarioBase.fontanero_id == fontanero_id
    ).all()

@app.post("/fontaneros/{fontanero_id}/bloqueos", response_model=schemas.BloqueoRespuesta)
def crear_bloqueo(
    fontanero_id: int,
    bloqueo: schemas.BloqueoCrear,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    nuevo = models.BloqueoHorario(
        fontanero_id=fontanero_id,
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
    return db.query(models.BloqueoHorario).filter(
        models.BloqueoHorario.fontanero_id == fontanero_id
    ).all()

@app.delete("/fontaneros/{fontanero_id}/bloqueos/{bloqueo_id}")
def eliminar_bloqueo(
    fontanero_id: int,
    bloqueo_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    bloqueo = db.query(models.BloqueoHorario).filter(
        models.BloqueoHorario.id == bloqueo_id,
        models.BloqueoHorario.fontanero_id == fontanero_id,
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
    nuevo = models.ServicioFontanero(
        fontanero_id=fontanero_id,
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
    return db.query(models.ServicioFontanero).filter(
        models.ServicioFontanero.fontanero_id == fontanero_id,
        models.ServicioFontanero.activo == True,
    ).all()

@app.delete("/fontaneros/{fontanero_id}/servicios/{servicio_id}")
def eliminar_servicio(
    fontanero_id: int,
    servicio_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    servicio = db.query(models.ServicioFontanero).filter(
        models.ServicioFontanero.id == servicio_id,
        models.ServicioFontanero.fontanero_id == fontanero_id,
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

@app.post("/servicios/{servicio_id}/mensajes", response_model=schemas.MensajeRespuesta)
def enviar_mensaje(
    servicio_id: int,
    datos: schemas.MensajeCrear,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    servicio = db.query(models.Servicio).filter(models.Servicio.id == servicio_id).first()
    if not servicio:
        raise HTTPException(status_code=404, detail="Servicio no encontrado")
    emisor_id = current_user["id"]
    mensaje = models.Mensaje(servicio_id=servicio_id, emisor_id=emisor_id, texto=datos.texto)
    db.add(mensaje)
    # notificar al otro participante
    if emisor_id == servicio.cliente_id and servicio.fontanero_id:
        fontanero_obj = db.query(models.Fontanero).filter(models.Fontanero.id == servicio.fontanero_id).first()
        if fontanero_obj and fontanero_obj.usuario_id:
            _crear_notificacion(db, fontanero_obj.usuario_id, "Nuevo mensaje", datos.texto[:80], "mensaje", servicio_id)
    else:
        _crear_notificacion(db, servicio.cliente_id, "Nuevo mensaje", datos.texto[:80], "mensaje", servicio_id)
    db.commit()
    db.refresh(mensaje)
    return mensaje

@app.get("/servicios/{servicio_id}/mensajes", response_model=List[schemas.MensajeRespuesta])
def listar_mensajes(
    servicio_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.get_current_user),
):
    return db.query(models.Mensaje).filter(
        models.Mensaje.servicio_id == servicio_id
    ).order_by(models.Mensaje.creado_en).all()

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