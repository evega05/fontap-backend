from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from typing import List, Optional
from pydantic import BaseModel
from . import models, schemas, auth
from .database import engine, get_db

models.Base.metadata.create_all(bind=engine)

app = FastAPI(title="FonTap API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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
    servicio.estado = "pago_pendiente" if datos.metodo == "efectivo" else "pagado"
    servicio.metodo_pago = datos.metodo
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