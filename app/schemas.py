# ══════════════════════════════════════════════════════════════════
# PARCHE schemas.py — modifica el schema Token para incluir id y email
# ══════════════════════════════════════════════════════════════════

from pydantic import BaseModel
from typing import Optional
import datetime

# ─── TOKEN (devuelto en login/registro) ───────────────────────────
class Token(BaseModel):
    access_token: str
    token_type: str
    tipo_usuario: str
    nombre: str
    id: int              # ← NUEVO: necesario para llamadas al backend
    email: str           # ← NUEVO

# ─── USUARIO ──────────────────────────────────────────────────────
class UsuarioRegistro(BaseModel):
    nombre: str
    email: str
    telefono: str
    password: str
    tipo: str = "cliente"

class UsuarioLogin(BaseModel):
    email: str
    password: str

# ─── FONTANERO ────────────────────────────────────────────────────
class FontaneroRespuesta(BaseModel):
    id: int
    nombre: str
    zona: Optional[str] = None
    disponible: bool
    disponible_24h: bool = False    # ← NUEVO
    valoracion: Optional[float] = None
    foto_url: Optional[str] = None

    class Config:
        from_attributes = True

# ─── SERVICIO ─────────────────────────────────────────────────────
class ServicioCrear(BaseModel):
    tipo: str
    descripcion: Optional[str] = None
    urgente: bool = False
    fecha: Optional[datetime.datetime] = None

class ServicioRespuesta(BaseModel):
    id: int
    tipo: str
    descripcion: Optional[str] = None
    urgente: bool
    estado: str
    precio: Optional[float] = None   # ← NUEVO
    fecha: Optional[datetime.datetime] = None

    class Config:
        from_attributes = True

# ─── SERVICIO FONTANERO ───────────────────────────────────────────
class ServicioFontaneroCrear(BaseModel):
    nombre: str
    precio: float
    duracion_minutos: int = 60

class ServicioFontaneroRespuesta(BaseModel):
    id: int
    nombre: str
    precio: float
    duracion_minutos: int
    activo: bool

    class Config:
        from_attributes = True

# ─── HORARIO ──────────────────────────────────────────────────────
class HorarioBaseCrear(BaseModel):
    dia_semana: int
    hora_inicio: str
    hora_fin: str
    intervalo_minutos: int = 60

class HorarioBaseRespuesta(BaseModel):
    id: int
    dia_semana: int
    hora_inicio: str
    hora_fin: str
    intervalo_minutos: int

    class Config:
        from_attributes = True

# ─── BLOQUEO ──────────────────────────────────────────────────────
class BloqueoCrear(BaseModel):
    fecha: datetime.datetime
    hora_inicio: str
    hora_fin: str
    motivo: Optional[str] = None

class BloqueoRespuesta(BaseModel):
    id: int
    fecha: datetime.datetime
    hora_inicio: str
    hora_fin: str
    motivo: Optional[str] = None

    class Config:
        from_attributes = True