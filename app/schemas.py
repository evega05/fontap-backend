from pydantic import BaseModel, EmailStr
from typing import Optional
from datetime import datetime

class UsuarioRegistro(BaseModel):
    nombre: str
    email: EmailStr
    telefono: str
    password: str
    tipo: str

class UsuarioLogin(BaseModel):
    email: EmailStr
    password: str

class UsuarioRespuesta(BaseModel):
    id: int
    nombre: str
    email: str
    telefono: str
    tipo: str
    foto_perfil: Optional[str] = None

    class Config:
        from_attributes = True

class FontaneroRegistro(BaseModel):
    zona: str
    latitud: Optional[float] = None
    longitud: Optional[float] = None

class FontaneroRespuesta(BaseModel):
    id: int
    usuario_id: int
    zona: str
    valoracion: float
    total_trabajos: int
    disponible: bool
    verificado: bool

    class Config:
        from_attributes = True

class ServicioCrear(BaseModel):
    tipo: str
    descripcion: Optional[str] = None
    urgente: bool = False
    fecha: Optional[datetime] = None

class ServicioRespuesta(BaseModel):
    id: int
    cliente_id: int
    fontanero_id: Optional[int] = None
    tipo: str
    urgente: bool
    estado: str
    precio: Optional[float] = None

    class Config:
        from_attributes = True

class Token(BaseModel):
    access_token: str
    token_type: str
    tipo_usuario: str
class HorarioBaseCrear(BaseModel):
    dia_semana: int  # 0=lunes, 6=domingo
    hora_inicio: str  # "08:00"
    hora_fin: str     # "18:00"
    intervalo_minutos: int = 60

class HorarioBaseRespuesta(BaseModel):
    id: int
    fontanero_id: int
    dia_semana: int
    hora_inicio: str
    hora_fin: str
    intervalo_minutos: int

    class Config:
        from_attributes = True

class BloqueoCrear(BaseModel):
    fecha: datetime
    hora_inicio: str
    hora_fin: str
    motivo: Optional[str] = None

class BloqueoRespuesta(BaseModel):
    id: int
    fontanero_id: int
    fecha: datetime
    hora_inicio: str
    hora_fin: str
    motivo: Optional[str] = None

    class Config:
        from_attributes = True

class ServicioFontaneroCrear(BaseModel):
    nombre: str
    precio: float
    duracion_minutos: int = 60

class ServicioFontaneroRespuesta(BaseModel):
    id: int
    fontanero_id: int
    nombre: str
    precio: float
    duracion_minutos: int
    activo: bool

    class Config:
        from_attributes = True