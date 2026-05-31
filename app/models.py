from sqlalchemy import Column, Integer, String, Boolean, Float, DateTime
from sqlalchemy.sql import func
from .database import Base

class Usuario(Base):
    __tablename__ = "usuarios"

    id = Column(Integer, primary_key=True, index=True)
    nombre = Column(String, nullable=False)
    email = Column(String, unique=True, index=True, nullable=False)
    telefono = Column(String, nullable=False)
    password_hash = Column(String, nullable=False)
    tipo = Column(String, nullable=False)  # "cliente" o "fontanero"
    foto_perfil = Column(String, nullable=True)
    creado_en = Column(DateTime, server_default=func.now())

class Fontanero(Base):
    __tablename__ = "fontaneros"

    id = Column(Integer, primary_key=True, index=True)
    usuario_id = Column(Integer, nullable=False)
    dni_foto = Column(String, nullable=True)
    zona = Column(String, nullable=False)
    valoracion = Column(Float, default=0.0)
    total_trabajos = Column(Integer, default=0)
    disponible = Column(Boolean, default=False)
    verificado = Column(Boolean, default=False)
    suscripcion_activa = Column(Boolean, default=False)
    latitud = Column(Float, nullable=True)
    longitud = Column(Float, nullable=True)

class Servicio(Base):
    __tablename__ = "servicios"

    id = Column(Integer, primary_key=True, index=True)
    cliente_id = Column(Integer, nullable=False)
    fontanero_id = Column(Integer, nullable=True)
    tipo = Column(String, nullable=False)
    descripcion = Column(String, nullable=True)
    urgente = Column(Boolean, default=False)
    estado = Column(String, default="pendiente")
    fecha = Column(DateTime, nullable=True)
    precio = Column(Float, nullable=True)
    valoracion_cliente = Column(Float, nullable=True)
    creado_en = Column(DateTime, server_default=func.now())
class HorarioBase(Base):
    __tablename__ = "horarios_base"

    id = Column(Integer, primary_key=True, index=True)
    fontanero_id = Column(Integer, nullable=False)
    dia_semana = Column(Integer, nullable=False)  # 0=lunes, 6=domingo
    hora_inicio = Column(String, nullable=False)  # "08:00"
    hora_fin = Column(String, nullable=False)      # "18:00"
    intervalo_minutos = Column(Integer, default=60)

class BloqueoHorario(Base):
    __tablename__ = "bloqueos_horario"

    id = Column(Integer, primary_key=True, index=True)
    fontanero_id = Column(Integer, nullable=False)
    fecha = Column(DateTime, nullable=False)
    hora_inicio = Column(String, nullable=False)
    hora_fin = Column(String, nullable=False)
    motivo = Column(String, nullable=True)

class ServicioFontanero(Base):
    __tablename__ = "servicios_fontanero"

    id = Column(Integer, primary_key=True, index=True)
    fontanero_id = Column(Integer, nullable=False)
    nombre = Column(String, nullable=False)
    precio = Column(Float, nullable=False)
    duracion_minutos = Column(Integer, default=60)
    activo = Column(Boolean, default=True)