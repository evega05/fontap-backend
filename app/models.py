# ══════════════════════════════════════════════════════════════════
# PARCHE models.py — añade estos campos a los modelos existentes
# Si ya tienes models.py, añade solo los campos marcados con ← NUEVO
# ══════════════════════════════════════════════════════════════════

# En clase Fontanero, añadir:
#   disponible_24h = Column(Boolean, default=False)    ← NUEVO

# En clase Servicio, añadir:
#   precio = Column(Float, nullable=True)              ← NUEVO (si no existe)
#   metodo_pago = Column(String, nullable=True)        ← NUEVO
#   estado default cambiar a "pendiente"               ← VERIFICAR

# ──────────────────────────────────────────────────────────────────
# MODELO COMPLETO SUGERIDO (reemplaza tu models.py entero):
# ──────────────────────────────────────────────────────────────────

from sqlalchemy import Column, Integer, String, Boolean, Float, DateTime, ForeignKey, Text
from sqlalchemy.orm import relationship
from .database import Base
import datetime

class Usuario(Base):
    __tablename__ = "usuarios"
    id = Column(Integer, primary_key=True, index=True)
    nombre = Column(String, nullable=False)
    email = Column(String, unique=True, index=True, nullable=False)
    telefono = Column(String)
    password_hash = Column(String, nullable=False)
    tipo = Column(String, default="cliente")       # "cliente" | "fontanero"
    creado_en = Column(DateTime, default=datetime.datetime.utcnow)

class Fontanero(Base):
    __tablename__ = "fontaneros"
    id = Column(Integer, primary_key=True, index=True)
    usuario_id = Column(Integer, ForeignKey("usuarios.id"), nullable=True)
    nombre = Column(String)
    telefono = Column(String)
    zona = Column(String, default="Bilbao")
    disponible = Column(Boolean, default=True)
    disponible_24h = Column(Boolean, default=False)    # ← NUEVO
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
    estado = Column(String, default="pendiente")   # pendiente | aceptado | precio_enviado | pagado | rechazado
    precio = Column(Float, nullable=True)          # ← NUEVO: lo pone el fontanero
    metodo_pago = Column(String, nullable=True)    # ← NUEVO
    fecha = Column(DateTime, nullable=True)
    creado_en = Column(DateTime, default=datetime.datetime.utcnow)

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
    dia_semana = Column(Integer)   # 0=lunes … 6=domingo
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