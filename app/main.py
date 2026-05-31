from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from typing import List
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
        tipo=usuario.tipo
    )
    db.add(nuevo)
    db.commit()
    db.refresh(nuevo)
    token = auth.crear_token({"sub": nuevo.email, "tipo": nuevo.tipo})
    return {"access_token": token, "token_type": "bearer", "tipo_usuario": nuevo.tipo, "nombre": nuevo.nombre}

@app.post("/login", response_model=schemas.Token)
def login(datos: schemas.UsuarioLogin, db: Session = Depends(get_db)):
    usuario = db.query(models.Usuario).filter(models.Usuario.email == datos.email).first()
    if not usuario or not auth.verificar_password(datos.password, usuario.password_hash):
        raise HTTPException(status_code=401, detail="Credenciales incorrectas")
    token = auth.crear_token({"sub": usuario.email, "tipo": usuario.tipo})
    return {"access_token": token, "token_type": "bearer", "tipo_usuario": usuario.tipo, "nombre": usuario.nombre}

@app.get("/fontaneros", response_model=List[schemas.FontaneroRespuesta])
def listar_fontaneros(db: Session = Depends(get_db)):
    return db.query(models.Fontanero).filter(models.Fontanero.disponible == True).all()

@app.post("/servicios", response_model=schemas.ServicioRespuesta)
def crear_servicio(servicio: schemas.ServicioCrear, cliente_id: int, db: Session = Depends(get_db)):
    nuevo = models.Servicio(
        cliente_id=cliente_id,
        tipo=servicio.tipo,
        descripcion=servicio.descripcion,
        urgente=servicio.urgente,
        fecha=servicio.fecha
    )
    db.add(nuevo)
    db.commit()
    db.refresh(nuevo)
    return nuevo

@app.put("/servicios/{servicio_id}/aceptar")
def aceptar_servicio(servicio_id: int, fontanero_id: int, db: Session = Depends(get_db)):
    servicio = db.query(models.Servicio).filter(models.Servicio.id == servicio_id).first()
    if not servicio:
        raise HTTPException(status_code=404, detail="Servicio no encontrado")
    servicio.fontanero_id = fontanero_id
    servicio.estado = "aceptado"
    db.commit()
    return {"mensaje": "Servicio aceptado"}

@app.put("/servicios/{servicio_id}/rechazar")
def rechazar_servicio(servicio_id: int, db: Session = Depends(get_db)):
    servicio = db.query(models.Servicio).filter(models.Servicio.id == servicio_id).first()
    if not servicio:
        raise HTTPException(status_code=404, detail="Servicio no encontrado")
    servicio.estado = "rechazado"
    db.commit()
    return {"mensaje": "Servicio rechazado"}
@app.post("/fontaneros/{fontanero_id}/horario", response_model=schemas.HorarioBaseRespuesta)
def crear_horario(fontanero_id: int, horario: schemas.HorarioBaseCrear, db: Session = Depends(get_db)):
    nuevo = models.HorarioBase(
        fontanero_id=fontanero_id,
        dia_semana=horario.dia_semana,
        hora_inicio=horario.hora_inicio,
        hora_fin=horario.hora_fin,
        intervalo_minutos=horario.intervalo_minutos
    )
    db.add(nuevo)
    db.commit()
    db.refresh(nuevo)
    return nuevo

@app.get("/fontaneros/{fontanero_id}/horario", response_model=List[schemas.HorarioBaseRespuesta])
def ver_horario(fontanero_id: int, db: Session = Depends(get_db)):
    return db.query(models.HorarioBase).filter(models.HorarioBase.fontanero_id == fontanero_id).all()

@app.post("/fontaneros/{fontanero_id}/bloqueos", response_model=schemas.BloqueoRespuesta)
def crear_bloqueo(fontanero_id: int, bloqueo: schemas.BloqueoCrear, db: Session = Depends(get_db)):
    nuevo = models.BloqueoHorario(
        fontanero_id=fontanero_id,
        fecha=bloqueo.fecha,
        hora_inicio=bloqueo.hora_inicio,
        hora_fin=bloqueo.hora_fin,
        motivo=bloqueo.motivo
    )
    db.add(nuevo)
    db.commit()
    db.refresh(nuevo)
    return nuevo

@app.get("/fontaneros/{fontanero_id}/bloqueos", response_model=List[schemas.BloqueoRespuesta])
def ver_bloqueos(fontanero_id: int, db: Session = Depends(get_db)):
    return db.query(models.BloqueoHorario).filter(models.BloqueoHorario.fontanero_id == fontanero_id).all()

@app.delete("/fontaneros/{fontanero_id}/bloqueos/{bloqueo_id}")
def eliminar_bloqueo(fontanero_id: int, bloqueo_id: int, db: Session = Depends(get_db)):
    bloqueo = db.query(models.BloqueoHorario).filter(
        models.BloqueoHorario.id == bloqueo_id,
        models.BloqueoHorario.fontanero_id == fontanero_id
    ).first()
    if not bloqueo:
        raise HTTPException(status_code=404, detail="Bloqueo no encontrado")
    db.delete(bloqueo)
    db.commit()
    return {"mensaje": "Bloqueo eliminado"}

@app.post("/fontaneros/{fontanero_id}/servicios", response_model=schemas.ServicioFontaneroRespuesta)
def añadir_servicio(fontanero_id: int, servicio: schemas.ServicioFontaneroCrear, db: Session = Depends(get_db)):
    nuevo = models.ServicioFontanero(
        fontanero_id=fontanero_id,
        nombre=servicio.nombre,
        precio=servicio.precio,
        duracion_minutos=servicio.duracion_minutos
    )
    db.add(nuevo)
    db.commit()
    db.refresh(nuevo)
    return nuevo

@app.get("/fontaneros/{fontanero_id}/servicios", response_model=List[schemas.ServicioFontaneroRespuesta])
def ver_servicios_fontanero(fontanero_id: int, db: Session = Depends(get_db)):
    return db.query(models.ServicioFontanero).filter(
        models.ServicioFontanero.fontanero_id == fontanero_id,
        models.ServicioFontanero.activo == True
    ).all()

@app.delete("/fontaneros/{fontanero_id}/servicios/{servicio_id}")
def eliminar_servicio(fontanero_id: int, servicio_id: int, db: Session = Depends(get_db)):
    servicio = db.query(models.ServicioFontanero).filter(
        models.ServicioFontanero.id == servicio_id,
        models.ServicioFontanero.fontanero_id == fontanero_id
    ).first()
    if not servicio:
        raise HTTPException(status_code=404, detail="Servicio no encontrado")
    servicio.activo = False
    db.commit()
    return {"mensaje": "Servicio eliminado"}