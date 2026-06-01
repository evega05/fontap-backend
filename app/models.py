from sqlalchemy import Column, Integer, String, Boolean, Float, DateTime, ForeignKey, Text
from sqlalchemy.orm import relationship
from .database import Base
import datetime

def utcnow():
    return datetime.datetime.now(datetime.timezone.utc)

class Usuario(Base):
    __tablename__ = "usuarios"
    id = Column(Integer, primary_key=True, index=True)
    nombre = Column(String, nullable=False)
    email = Column(String, unique=True, index=True, nullable=False)
    telefono = Column(String)
    password_hash = Column(String, nullable=False)
    tipo = Column(String, default="cliente")
    creado_en = Column(DateTime, default=utcnow)

class Fontanero(Base):
    __tablename__ = "fontaneros"
    id = Column(Integer, primary_key=True, index=True)
    usuario_id = Column(Integer, ForeignKey("usuarios.id"), nullable=True)
    nombre = Column(String)
    telefono = Column(String)
    zona = Column(String, default="Bilbao")
    disponible = Column(Boolean, default=True)
    disponible_24h = Column(Boolean, default=False)
    valoracion = Column(Float, default=5.0)
    foto_url = Column(String, nullable=True)

class Servicio(Base):
    __tablename__ = "servicios"
    id = Column(Integer, primary_key=True, index=True)
    cliente_id = Column(Integer, ForeignKey("usuarios.id"))
    fontanero_id = Column(Integer, ForeignKey("fontaneros.id"), nullable=True)
    tipo = Column(String)
    descripcion = Column(Text, nullable=True)
    urgente = Column(Boolean, default=False)
    estado = Column(String, default="pendiente")
    precio = Column(Float, nullable=True)
    metodo_pago = Column(String, nullable=True)
    fecha = Column(DateTime, nullable=True)
    creado_en = Column(DateTime, default=utcnow)

class ServicioFontanero(Base):
    __tablename__ = "servicios_fontanero"
    id = Column(Integer, primary_key=True, index=True)
    fontanero_id = Column(Integer, ForeignKey("fontaneros.id"))
    nombre = Column(String)
    precio = Column(Float)
    duracion_minutos = Column(Integer, default=60)
    activo = Column(Boolean, default=True)

class HorarioBase(Base):
    __tablename__ = "horarios_base"
    id = Column(Integer, primary_key=True, index=True)
    fontanero_id = Column(Integer, ForeignKey("fontaneros.id"))
    dia_semana = Column(Integer)
    hora_inicio = Column(String)
    hora_fin = Column(String)
    intervalo_minutos = Column(Integer, default=60)

class BloqueoHorario(Base):
    __tablename__ = "bloqueos_horario"
    id = Column(Integer, primary_key=True, index=True)
    fontanero_id = Column(Integer, ForeignKey("fontaneros.id"))
    fecha = Column(DateTime)
    hora_inicio = Column(String)
    hora_fin = Column(String)
    motivo = Column(String, nullable=True)

class ImagenServicio(Base):
    __tablename__ = "imagenes_servicio"
    id = Column(Integer, primary_key=True, index=True)
    servicio_id = Column(Integer, ForeignKey("servicios.id"))
    url = Column(String)
    creado_en = Column(DateTime, default=utcnow)

class Mensaje(Base):
    __tablename__ = "mensajes"
    id = Column(Integer, primary_key=True, index=True)
    servicio_id = Column(Integer, ForeignKey("servicios.id"))
    emisor_id = Column(Integer, ForeignKey("usuarios.id"))
    texto = Column(Text)
    leido = Column(Boolean, default=False)
    creado_en = Column(DateTime, default=utcnow)

class TokenPush(Base):
    __tablename__ = "tokens_push"
    id = Column(Integer, primary_key=True, index=True)
    usuario_id = Column(Integer, ForeignKey("usuarios.id"))
    token = Column(String, unique=True)
    plataforma = Column(String, default="android")
    activo = Column(Boolean, default=True)
    creado_en = Column(DateTime, default=utcnow)

class Notificacion(Base):
    __tablename__ = "notificaciones"
    id = Column(Integer, primary_key=True, index=True)
    usuario_id = Column(Integer, ForeignKey("usuarios.id"))
    titulo = Column(String)
    cuerpo = Column(String)
    tipo = Column(String, nullable=True)
    referencia_id = Column(Integer, nullable=True)
    leida = Column(Boolean, default=False)
    creado_en = Column(DateTime, default=utcnow)